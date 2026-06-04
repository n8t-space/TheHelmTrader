# Launch an isolated Helm DEV instance.
#
# Independent from the live Helm: its own HELM_HOME (settings + credentials),
# its own port, run in the FOREGROUND (Ctrl+C to stop) -- it is NOT the NSSM
# watchdog service and does not touch the live :8000 instance. Runtime data
# (trades.db, feed.db, signals.jsonl) is whatever lives in THIS checkout, so
# run this from the dev worktree created by setup-dev-env.ps1.
#
# Usage (from the dev worktree's Trade_Perf):
#   .\runtime\run-dev.ps1                 # http://127.0.0.1:8001
#   .\runtime\run-dev.ps1 -Port 8002
[CmdletBinding()]
param(
    [int]$Port = 8001,
    [string]$HelmHome = (Join-Path $env:USERPROFILE '.helm-dev')
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path "$PSScriptRoot\..").Path   # the Trade_Perf dir of this checkout

$env:HELM_HOME = $HelmHome
New-Item -ItemType Directory -Force -Path $HelmHome | Out-Null

# Resolve a service-usable python the same way watchdog.ps1 does.
if (Test-Path 'C:\Windows\py.exe') {
    $py = 'C:\Windows\py.exe'
    $pyArgs = @('-3','-m','uvicorn','dashboard.api.main:app','--host','127.0.0.1','--port',$Port,'--reload')
} else {
    $py = 'python'
    $pyArgs = @('-m','uvicorn','dashboard.api.main:app','--host','127.0.0.1','--port',$Port,'--reload')
}

Write-Host '=== Helm DEV instance ==='
Write-Host "  HELM_HOME : $HelmHome"
Write-Host "  port      : $Port   (live is 8000 -- untouched)"
Write-Host "  cwd       : $root"
Write-Host "  reload    : on   (edits restart uvicorn)"
Write-Host '  Ctrl+C to stop. This is NOT the NSSM service.'
Write-Host ''

Push-Location $root
try { & $py @pyArgs } finally { Pop-Location }
