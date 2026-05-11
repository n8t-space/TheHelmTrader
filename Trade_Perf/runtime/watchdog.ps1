# Trade_Perf watchdog
#
# Polls every 5s for NinjaTrader.exe. When NT is running, ensures the unified
# dashboard (FastAPI on :8000, serving the built React frontend) is also
# running. When NT exits, stops the dashboard.
#
# Designed to be launched at user logon by the Task Scheduler entry installed
# via runtime/install_watchdog.ps1. Runs forever; resilient to transient
# errors (caught and logged, loop continues).
#
# Manual run for debugging:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File watchdog.ps1

[CmdletBinding()]
param(
    [int]$IntervalSeconds = 5,
    [string]$ProcessName = 'NinjaTrader',
    [int]$Port = 8000
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

$projectRoot = (Resolve-Path "$PSScriptRoot\..").Path
$logFile     = Join-Path $projectRoot 'data\watchdog.log'
$null        = New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null

function Write-Log {
    param([string]$Msg, [string]$Level = 'INFO')
    $line = '{0} {1} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Msg
    Write-Output $line
    try { Add-Content -Path $logFile -Value $line -ErrorAction Stop } catch { }
}

function Test-PortInUse([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# Resolve a real python.exe usable from a Windows Service context. The
# bare 'python' command on PATH typically points at a WindowsApps app
# execution alias (e.g. %USERPROFILE%\AppData\Local\Microsoft\WindowsApps\python.exe)
# which fails with 'Access is denied' when invoked from a non-interactive
# session. Prefer concrete .exe paths in this order:
#   1. py launcher (C:\Windows\py.exe) -- always available, works from
#      Session 0, picks the highest-version Python by default.
#   2. The real Store Python binary behind the app alias.
#   3. Anything on PATH whose path is NOT under WindowsApps.
function Resolve-PythonExe {
    $py = 'C:\Windows\py.exe'
    if (Test-Path $py) { return @{ Exe = $py; UseLauncher = $true } }

    # Skip anything under WindowsApps — even non-alias binaries there have
    # user-session-only ACLs that block service-context access.
    $candidates = @(
        # Per-user winget Python install
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python311\python.exe'),
        # Machine-wide installs (winget --scope=machine, or python.org installer)
        'C:\Program Files\Python313\python.exe',
        'C:\Program Files\Python312\python.exe',
        'C:\Program Files\Python311\python.exe',
        'C:\Python313\python.exe',
        'C:\Python312\python.exe',
        'C:\Python311\python.exe'
    )
    foreach ($c in $candidates) {
        if (Test-Path $c) { return @{ Exe = $c; UseLauncher = $false } }
    }

    # Last resort: look on PATH but skip ANY WindowsApps path (alias or
    # actual binary — both fail in service context).
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch 'WindowsApps' } |
        Select-Object -First 1
    if ($cmd) { return @{ Exe = $cmd.Source; UseLauncher = $false } }

    return $null
}

function Start-Dashboard {
    $resolved = Resolve-PythonExe
    if (-not $resolved) {
        Write-Log "FATAL: could not resolve a service-usable python.exe (PATH lookup hit WindowsApps alias only). Install Python via 'winget install Python.Python.3.12' or run 'py -V' to confirm py launcher exists." 'ERROR'
        return $null
    }

    if ($resolved.UseLauncher) {
        $procArgs = @('-3','-m','uvicorn','dashboard.api.main:app','--host','127.0.0.1','--port',$Port)
    } else {
        $procArgs = @('-m','uvicorn','dashboard.api.main:app','--host','127.0.0.1','--port',$Port)
    }

    Write-Log "starting uvicorn on port $Port via $($resolved.Exe) (cwd=$projectRoot)"
    $proc = Start-Process `
        -FilePath  $resolved.Exe `
        -ArgumentList $procArgs `
        -WorkingDirectory $projectRoot `
        -WindowStyle Hidden `
        -PassThru
    Write-Log "uvicorn started (PID=$($proc.Id))"
    return $proc
}

function Stop-Dashboard($proc) {
    if ($null -eq $proc) { return }
    if ($proc.HasExited) {
        Write-Log "dashboard already exited (PID=$($proc.Id), exit=$($proc.ExitCode))"
        return
    }
    Write-Log "stopping dashboard (PID=$($proc.Id))"
    try {
        Stop-Process -Id $proc.Id -Force -ErrorAction Stop
        $proc.WaitForExit(5000) | Out-Null
        Write-Log "dashboard stopped"
    } catch {
        Write-Log "stop failed: $_" 'ERROR'
    }
}

function Get-NtProcess {
    return Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
}

# ---------- main loop ----------

Write-Log "watchdog starting (process=$ProcessName, port=$Port, interval=${IntervalSeconds}s)"

$dashboard = $null

# Recover from a previous instance: if uvicorn is already listening on the
# port (e.g. user manually started it, or the watchdog was killed without
# stopping it), don't double-start. Adopt nothing -- leave it alone.
if (Test-PortInUse $Port) {
    Write-Log "port $Port already in use; not adopting an unknown process"
    $dashboard = $null  # we won't manage it
}

while ($true) {
    try {
        $nt = Get-NtProcess
        $dashAlive = ($null -ne $dashboard) -and -not $dashboard.HasExited

        if ($nt -and -not $dashAlive) {
            if (Test-PortInUse $Port) {
                Write-Log "NinjaTrader detected, but port $Port already in use -- skipping start"
            } else {
                Write-Log "NinjaTrader detected (PID=$($nt[0].Id)); bringing dashboard up"
                $dashboard = Start-Dashboard
            }
        }
        elseif (-not $nt -and $dashAlive) {
            Write-Log "NinjaTrader stopped; bringing dashboard down"
            Stop-Dashboard $dashboard
            $dashboard = $null
        }
    } catch {
        Write-Log "loop error: $_" 'ERROR'
    }

    Start-Sleep -Seconds $IntervalSeconds
}
