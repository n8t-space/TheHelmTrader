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

# Kill-switch sentinel, written by POST /api/control/kill. Beside settings.json
# in ~/.helm so the API and watchdog resolve the same path.
$helmHome       = if ($env:HELM_HOME) { $env:HELM_HOME } else { Join-Path $env:USERPROFILE '.helm' }
$killSwitchPath = Join-Path $helmHome 'kill-switch.json'

function Write-Log {
    param([string]$Msg, [string]$Level = 'INFO')
    $line = '{0} {1} {2}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Level, $Msg
    # Write-Host (not Write-Output): Write-Output emits onto the success
    # pipeline, so any function that logs then returns an object (Start-Dashboard)
    # leaks the log strings into its return value. $dashboard then became
    # @(string, string, Process) and Stop-Dashboard's $proc.WaitForExit() blew up
    # on a [String]. Write-Host goes to the host stream and never pollutes returns.
    Write-Host $line
    try { Add-Content -Path $logFile -Value $line -ErrorAction Stop } catch { }
}

function Test-PortInUse([int]$port) {
    return [bool](Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue)
}

# Resolve a real python.exe usable from a Windows Service context. The
# bare 'python' command on PATH typically points at a WindowsApps app
# execution alias (e.g. %LOCALAPPDATA%\Microsoft\WindowsApps\python.exe)
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
    # Defensive: if a caller ever hands a polluted value (array/string), dig out
    # the real Process so we never invoke .HasExited/.WaitForExit on a [String].
    if ($proc -isnot [System.Diagnostics.Process]) {
        $proc = @($proc) | Where-Object { $_ -is [System.Diagnostics.Process] } | Select-Object -First 1
        if ($null -eq $proc) { Write-Log 'stop skipped: no Process handle' 'ERROR'; return }
    }
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

function Stop-DashboardByPort([int]$port) {
    # Kill whatever is listening on the port, even if this watchdog didn't start
    # it (adopted/unmanaged instance). Used by the kill switch so the stop is
    # guaranteed regardless of who owns uvicorn.
    $conns = Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue
    foreach ($c in $conns) {
        try {
            Stop-Process -Id $c.OwningProcess -Force -ErrorAction Stop
            Write-Log "kill switch: stopped PID $($c.OwningProcess) on port $port"
        } catch {
            Write-Log "kill switch: stop-by-port failed for PID $($c.OwningProcess): $_" 'ERROR'
        }
    }
}

function Get-NtProcess {
    return Get-Process -Name $ProcessName -ErrorAction SilentlyContinue
}

# Returns $true while the kill switch should keep the dashboard DOWN. Stamps the
# armed sentinel with the NT instance live at kill time (PID + start time), then
# lifts the switch (deletes the file) once that instance is gone -- i.e. NT
# restarted or closed. A malformed/unstampable sentinel is cleared defensively.
function Test-KillSwitch($nt) {
    if (-not (Test-Path $killSwitchPath)) { return $false }

    $data = $null
    try { $data = Get-Content -Path $killSwitchPath -Raw -ErrorAction Stop | ConvertFrom-Json } catch { }
    if ($null -eq $data) {
        Write-Log "kill switch: unreadable sentinel, clearing" 'ERROR'
        Remove-Item $killSwitchPath -Force -ErrorAction SilentlyContinue
        return $false
    }

    $ntStart = if ($nt) { $nt[0].StartTime.ToString('o') } else { $null }

    if (-not $data.nt_pid) {
        # First sighting: pin to the currently-running NT instance.
        if ($nt) {
            $data | Add-Member -NotePropertyName nt_pid   -NotePropertyValue $nt[0].Id    -Force
            $data | Add-Member -NotePropertyName nt_start -NotePropertyValue $ntStart      -Force
            $data | ConvertTo-Json -Compress | Set-Content -Path $killSwitchPath -Encoding UTF8
            Write-Log "kill switch armed; pinned to NinjaTrader PID $($nt[0].Id)"
            return $true
        }
        # Nothing to pin to (NT not running) -- the pause has no anchor; clear it.
        Write-Log "kill switch: no NinjaTrader to pin to, clearing"
        Remove-Item $killSwitchPath -Force -ErrorAction SilentlyContinue
        return $false
    }

    # Already pinned: still the same NT instance?
    $sameNt = $nt -and ($nt[0].Id -eq $data.nt_pid) -and ($ntStart -eq $data.nt_start)
    if ($sameNt) { return $true }

    Write-Log "kill switch: NinjaTrader restarted/closed; lifting"
    Remove-Item $killSwitchPath -Force -ErrorAction SilentlyContinue
    return $false
}

# ---------- main loop ----------

Write-Log "watchdog starting (process=$ProcessName, port=$Port, interval=${IntervalSeconds}s)"

# A manual (or boot) service start resumes the Helm: drop any kill switch left
# over from a prior watchdog lifetime.
if (Test-Path $killSwitchPath) {
    Remove-Item $killSwitchPath -Force -ErrorAction SilentlyContinue
    Write-Log "cleared stale kill switch on startup (service start resumes Helm)"
}

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

        if (Test-KillSwitch $nt) {
            # Kill switch armed: keep the dashboard down regardless of NT.
            if ($dashAlive) {
                Write-Log "kill switch armed; bringing dashboard down"
                Stop-Dashboard $dashboard
                $dashboard = $null
            }
            elseif (Test-PortInUse $Port) {
                Stop-DashboardByPort $Port
            }
        }
        elseif ($nt -and -not $dashAlive) {
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
