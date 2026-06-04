# One-time setup of an isolated Helm DEV environment.
#
# Creates a git worktree on a 'dev' branch beside the live repo, seeds an
# isolated ~/.helm-dev (settings + credentials + news cache), and copies a
# SNAPSHOT of live runtime data (trades.db, feed.db, signals, screenshots,
# built frontend) into the dev tree so you can test against real history
# without touching the live instance. Re-runnable; refreshes the data copy.
#
# Run from the LIVE checkout:
#   .\Trade_Perf\runtime\setup-dev-env.ps1
# Then:
#   cd ..\TheHelmTrader-dev\Trade_Perf ; .\runtime\run-dev.ps1
[CmdletBinding()]
param(
    [string]$DevHome = (Join-Path $env:USERPROFILE '.helm-dev')
)

$ErrorActionPreference = 'Stop'
$repo     = (Resolve-Path "$PSScriptRoot\..\..").Path            # TheHelmTrader (live)
$worktree = Join-Path (Split-Path $repo -Parent) 'TheHelmTrader-dev'

Write-Host '=== Helm DEV environment setup ==='
Write-Host "  live repo : $repo"
Write-Host "  dev tree  : $worktree"
Write-Host "  dev home  : $DevHome"
Write-Host ''

# 1. git worktree on a 'dev' branch (idempotent).
if (Test-Path $worktree) {
    Write-Host "[1/4] worktree exists; skipping create"
} else {
    Write-Host "[1/4] creating worktree + 'dev' branch"
    git -C $repo show-ref --verify --quiet refs/heads/dev
    if ($?) { git -C $repo worktree add $worktree dev }
    else    { git -C $repo worktree add -b dev $worktree }
}

# 2. isolated ~/.helm-dev seeded from live (so the AI backend + accounts work).
Write-Host "[2/4] seeding $DevHome from ~/.helm"
New-Item -ItemType Directory -Force -Path $DevHome | Out-Null
foreach ($f in 'settings.json','credentials.json','news-cache.json') {
    $src = Join-Path $env:USERPROFILE ".helm\$f"
    if (Test-Path $src) { Copy-Item $src (Join-Path $DevHome $f) -Force }
}

# 3. snapshot live runtime data into the dev tree.
Write-Host "[3/4] copying live runtime data snapshot"
$liveTP  = Join-Path $repo     'Trade_Perf'
$devTP   = Join-Path $worktree 'Trade_Perf'
if (Test-Path (Join-Path $liveTP 'trades.db')) {
    Copy-Item (Join-Path $liveTP 'trades.db') (Join-Path $devTP 'trades.db') -Force
}
$liveData = Join-Path $repo     'TradingBot\app\data'
$devData  = Join-Path $worktree 'TradingBot\app\data'
New-Item -ItemType Directory -Force -Path $devData | Out-Null
foreach ($f in 'feed.db','feed.db-wal','signals.jsonl') {
    $src = Join-Path $liveData $f
    if (Test-Path $src) { Copy-Item $src (Join-Path $devData $f) -Force }
}
$liveShots = Join-Path $liveData 'screenshots'
if (Test-Path $liveShots) { Copy-Item $liveShots $devData -Recurse -Force }

# 4. copy the built frontend so the dev UI works immediately (rebuild with
#    'npm run build' in the dev tree's dashboard/web after frontend edits).
Write-Host "[4/4] copying built frontend (dist)"
$liveDist = Join-Path $liveTP 'dashboard\web\dist'
$devWeb   = Join-Path $devTP  'dashboard\web'
if (Test-Path $liveDist) {
    New-Item -ItemType Directory -Force -Path $devWeb | Out-Null
    Copy-Item $liveDist $devWeb -Recurse -Force
}

Write-Host ''
Write-Host 'Done. Launch the dev instance with:'
Write-Host "  cd `"$devTP`""
Write-Host '  .\runtime\run-dev.ps1            # http://127.0.0.1:8001'
Write-Host ''
Write-Host 'The dev instance uses ~/.helm-dev + this snapshot; live (:8000) is untouched.'
