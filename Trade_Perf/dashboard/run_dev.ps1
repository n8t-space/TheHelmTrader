# Launches the FastAPI backend (port 8000) and the Vite dev server (port 5173)
# in two new console windows, then opens the dashboard in the default browser.
#
# Usage:  pwsh -ExecutionPolicy Bypass -File run_dev.ps1
#
# Closing either console window stops that server. Re-running this script while
# the ports are still in use will fail; close the existing windows first.

[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'

$projectRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$webDir      = Join-Path $projectRoot 'dashboard\web'

# Backend in its own window so logs are visible
Start-Process -FilePath 'powershell.exe' -ArgumentList @(
    '-NoExit', '-Command',
    "Set-Location -Path '$projectRoot'; python -m uvicorn dashboard.api.main:app --port 8000 --reload"
) -WindowStyle Normal | Out-Null
Write-Host "Started backend in a new window (port 8000)."

# Frontend in another window
Start-Process -FilePath 'powershell.exe' -ArgumentList @(
    '-NoExit', '-Command',
    "Set-Location -Path '$webDir'; npm run dev"
) -WindowStyle Normal | Out-Null
Write-Host "Started Vite dev server in a new window (port 5173)."

# Wait briefly for ports to come up, then open the browser
Start-Sleep -Seconds 3
Start-Process 'http://localhost:5173/'
Write-Host ""
Write-Host "Dashboard: http://localhost:5173/"
Write-Host "API docs:  http://localhost:8000/docs"
Write-Host ""
Write-Host "Close the two PowerShell windows to stop the servers."
