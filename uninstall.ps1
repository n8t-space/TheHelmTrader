# uninstall.ps1 -- The Helm one-shot uninstaller
#
# Run from elevated PowerShell, inside a TheHelmTrader checkout:
#
#     cd $HOME\Documents\Projects\TheHelmTrader
#     # Right-click PowerShell -> Run as administrator
#     .\uninstall.ps1
#
# What it does (default):
#   1. Stops + removes the HelmDashboardWatchdog NSSM service
#   2. Stops the recorder.py background process + removes its Startup shortcut
#   3. Removes the NinjaScript indicators from NT8's Indicators folder
#
# What it preserves (default):
#   - %USERPROFILE%\.helm\settings.json   (dashboard config + API keys)
#   - Trade_Perf\trades.db                (NT8 fill mirror)
#   - TradingBot\app\data\signals.jsonl   (LLM proposals + journal)
#   - TradingBot\app\data\feed.db         (live bars + ticks)
#
# Idempotent: re-runnable. Each step skips work already done.
#
# Switches:
#   -SkipService        Don't touch the NSSM service
#   -SkipRecorder       Don't touch the recorder process or Startup shortcut
#   -SkipNsIndicators   Don't touch NT8's Indicators folder
#   -PurgeSettings      Also remove %USERPROFILE%\.helm
#   -PurgeData          Also remove trades.db, signals.jsonl, feed.db
#   -All                Shortcut for -PurgeSettings + -PurgeData
#
# ASCII-only by design (PS 5.1 silently mishandles non-ASCII in scripts).

[CmdletBinding()]
param(
    [switch]$SkipService,
    [switch]$SkipRecorder,
    [switch]$SkipNsIndicators,
    [switch]$PurgeSettings,
    [switch]$PurgeData,
    [switch]$All
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

if ($All) {
    $PurgeSettings = $true
    $PurgeData = $true
}

# ----- Sanity ----------------------------------------------------------------

if (-not (Test-Path "$here\TradingBot\app\src") -or -not (Test-Path "$here\Trade_Perf\dashboard\api")) {
    throw "uninstall.ps1 must run from the root of a TheHelmTrader checkout. Got: $here"
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run from elevated PowerShell (right-click PowerShell or Terminal -> Run as administrator)."
}

Write-Host "==================================================="
Write-Host " The Helm uninstaller"
Write-Host " Checkout : $here"
if ($PurgeSettings) { Write-Host " PurgeSettings : YES (will delete ~/.helm)" }
if ($PurgeData)     { Write-Host " PurgeData     : YES (will delete trades.db, signals.jsonl, feed.db)" }
Write-Host "==================================================="

# ----- 1. NSSM service -------------------------------------------------------

if (-not $SkipService) {
    Write-Host ""
    Write-Host "[1/5] Removing HelmDashboardWatchdog NSSM service ..."
    $svcScript = "$here\Trade_Perf\runtime\uninstall_service.ps1"
    if (Test-Path $svcScript) {
        & $svcScript
    } else {
        Write-Host "   uninstall_service.ps1 not found; trying direct service removal."
        $svc = Get-Service -Name 'HelmDashboardWatchdog' -ErrorAction SilentlyContinue
        if ($svc) {
            if ($svc.Status -ne 'Stopped') { Stop-Service 'HelmDashboardWatchdog' -Force -ErrorAction SilentlyContinue }
            sc.exe delete HelmDashboardWatchdog | Out-Null
        } else {
            Write-Host "   service not present; nothing to do."
        }
    }
} else {
    Write-Host ""
    Write-Host "[1/5] Skipping NSSM service (per -SkipService)."
}

# ----- 2. Recorder process + Startup shortcut --------------------------------

if (-not $SkipRecorder) {
    Write-Host ""
    Write-Host "[2/5] Stopping recorder + removing Startup shortcut ..."

    $running = Get-CimInstance Win32_Process -ErrorAction SilentlyContinue | Where-Object { $_.CommandLine -like '*recorder.py*' }
    if ($running) {
        foreach ($p in $running) {
            Write-Host "   stopping recorder PID $($p.ProcessId)"
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
    } else {
        Write-Host "   recorder process not running."
    }

    $startup = [Environment]::GetFolderPath('Startup')
    $shortcut = Join-Path $startup 'NT8 Trade Recorder.lnk'
    if (Test-Path $shortcut) {
        Remove-Item -Force $shortcut
        Write-Host "   removed Startup shortcut: $shortcut"
    } else {
        Write-Host "   Startup shortcut not present."
    }
} else {
    Write-Host ""
    Write-Host "[2/5] Skipping recorder (per -SkipRecorder)."
}

# ----- 3. NinjaScript indicators --------------------------------------------

if (-not $SkipNsIndicators) {
    Write-Host ""
    Write-Host "[3/5] Removing NinjaScript indicators ..."
    $ns = "$HOME\Documents\NinjaTrader 8\bin\Custom\Indicators\_Helm Locker"
    if (Test-Path $ns) {
        Remove-Item -Recurse -Force $ns
        Write-Host "   removed: $ns"
        Write-Host "   Reminder: in NinjaTrader, NinjaScript Editor (F11) -> Compile (F5) to clear the compiled assembly."
    } else {
        Write-Host "   indicators folder not present."
    }
} else {
    Write-Host ""
    Write-Host "[3/5] Skipping NinjaScript indicators (per -SkipNsIndicators)."
}

# ----- 4. Settings (~/.helm) -- opt-in --------------------------------------

Write-Host ""
if ($PurgeSettings) {
    Write-Host "[4/5] Removing settings ($HOME\.helm) ..."
    $cfg = "$HOME\.helm"
    if (Test-Path $cfg) {
        Remove-Item -Recurse -Force $cfg
        Write-Host "   removed: $cfg"
    } else {
        Write-Host "   settings folder not present."
    }
} else {
    Write-Host "[4/5] Preserving settings ($HOME\.helm). Pass -PurgeSettings to delete."
}

# ----- 5. Data files -- opt-in ----------------------------------------------

Write-Host ""
if ($PurgeData) {
    Write-Host "[5/5] Removing data files ..."
    $targets = @(
        "$here\Trade_Perf\trades.db",
        "$here\TradingBot\app\data\signals.jsonl",
        "$here\TradingBot\app\data\feed.db"
    )
    foreach ($t in $targets) {
        if (Test-Path $t) {
            Remove-Item -Force $t
            Write-Host "   removed: $t"
        } else {
            Write-Host "   not present: $t"
        }
    }
} else {
    Write-Host "[5/5] Preserving data (trades.db, signals.jsonl, feed.db). Pass -PurgeData to delete."
}

# ----- Done ------------------------------------------------------------------

Write-Host ""
Write-Host "==================================================="
Write-Host " Uninstall complete."
Write-Host ""
Write-Host " The checkout at $here is untouched."
Write-Host " Delete it by hand if you want a clean wipe:"
Write-Host "   Remove-Item -Recurse -Force '$here'"
Write-Host "==================================================="
