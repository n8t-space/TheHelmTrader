#Requires -Version 5.1
<#
.SYNOPSIS
    Pre-push gate. Runs the fast, deterministic test core plus the frontend
    production build. Exits non-zero if anything fails so a broken push is
    blocked.

.DESCRIPTION
    Dev tooling -- NOT shipped with the app. Invoked by .git/hooks/pre-push and
    runnable by hand:  powershell -File scripts/preflight.ps1

    Each test tree is run separately so its own pytest.ini (asyncio mode, marker
    registration) applies. Slow live-data tests are marked 'integration' and are
    excluded from the default gate; pass -Full to include them.

    Stages:
      1. pytest Trade_Perf/tests   (-m "not integration" unless -Full)
      2. pytest TradingBot/app/tests (-m "not integration" unless -Full)
      3. npm run build (tsc -b && vite build)

.PARAMETER Full
    Include the 'integration' tests (live feed.db, worker spawns -- minutes, and
    timing-coupled). Use before a release, not on every push.

.PARAMETER SkipBuild
    Run only the Python core (fast inner loop). The pre-push hook never sets this.
#>
[CmdletBinding()]
param(
    [switch]$Full,
    [switch]$SkipBuild
)

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$failures = New-Object System.Collections.Generic.List[string]
$markExpr = if ($Full) { $null } else { 'not integration' }

function Write-Stage([string]$msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

function Invoke-Suite([string]$label, [string]$path) {
    Write-Stage "Preflight: $label"
    Push-Location $repo
    try {
        if ($markExpr) {
            & python -m pytest $path -q -m $markExpr
        } else {
            & python -m pytest $path -q
        }
        if ($LASTEXITCODE -ne 0) { $script:failures.Add($label) }
    } finally {
        Pop-Location
    }
}

Invoke-Suite 'backend core (Trade_Perf)' 'Trade_Perf/tests'
Invoke-Suite 'backend core (TradingBot)' 'TradingBot/app/tests'

if (-not $SkipBuild) {
    Write-Stage 'Preflight: frontend build'
    Push-Location (Join-Path $repo 'Trade_Perf/dashboard/web')
    try {
        & npm run build
        if ($LASTEXITCODE -ne 0) { $failures.Add('frontend build') }
    } finally {
        Pop-Location
    }
}

Write-Host ''
if ($failures.Count -gt 0) {
    Write-Host "PREFLIGHT FAILED: $($failures -join ', ')" -ForegroundColor Red
    exit 1
}
Write-Host 'PREFLIGHT PASSED' -ForegroundColor Green
exit 0
