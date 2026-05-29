# update.ps1 -- one-click in-place updater for The Helm dashboard.
#
# Spawned detached by POST /api/version/update. Writes progress to a JSON
# status file the frontend polls, then kills uvicorn at the end. The watchdog
# notices the dead uvicorn within ~5s and brings it back up with the new code.
#
# Run manually for debugging:
#   pwsh -NoProfile -ExecutionPolicy Bypass -File update.ps1 -UvicornPid <pid>
#
# ASCII-only on purpose (PS 5.1 mishandles non-ASCII in unsigned scripts).

[CmdletBinding()]
param(
    [Parameter(Mandatory)][int]$UvicornPid,
    [string]$RepoRoot   = $null,
    [string]$StatusPath = (Join-Path $env:USERPROFILE '.helm\update-status.json'),
    [string]$Remote     = 'origin',
    [string]$Branch     = 'main',
    [int]$RestartGraceSeconds = 1
)

$ErrorActionPreference = 'Continue'
$ProgressPreference    = 'SilentlyContinue'

if (-not $RepoRoot) {
    # runtime/update.ps1 -> runtime -> Trade_Perf -> repo root
    $RepoRoot = (Resolve-Path "$PSScriptRoot\..\..").Path
}

$logPath = Join-Path $RepoRoot 'data\update.log'
$null = New-Item -ItemType Directory -Force -Path (Split-Path $logPath)  | Out-Null
$null = New-Item -ItemType Directory -Force -Path (Split-Path $StatusPath) | Out-Null

function Write-UpdateLog {
    param([string]$Msg)
    $line = '{0}  {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Msg
    try { Add-Content -Path $logPath -Value $line -ErrorAction Stop } catch { }
}

$script:tail = New-Object System.Collections.Generic.Queue[string]
function Add-Tail {
    param([string]$Msg)
    foreach ($line in ($Msg -split "`r?`n")) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $script:tail.Enqueue($line)
        while ($script:tail.Count -gt 40) { [void]$script:tail.Dequeue() }
    }
}

function Write-Status {
    param(
        [string]$Stage,
        [string]$Message,
        [int]$Step,
        [int]$TotalSteps,
        [string]$Error = $null,
        [string]$TargetSha = $null
    )
    $payload = [ordered]@{
        stage        = $Stage
        message      = $Message
        step         = $Step
        total_steps  = $TotalSteps
        log_tail     = @($script:tail.ToArray())
        started_at   = $script:startedAt
        finished_at  = if ($Stage -in @('done','failed')) { (Get-Date).ToString('o') } else { $null }
        error        = $Error
        target_sha   = $TargetSha
        pid          = $PID
    }
    $json = $payload | ConvertTo-Json -Depth 4 -Compress
    $tmp  = "$StatusPath.tmp"
    try {
        Set-Content -Path $tmp -Value $json -Encoding utf8 -ErrorAction Stop
        Move-Item -Path $tmp -Destination $StatusPath -Force -ErrorAction Stop
    } catch {
        Write-UpdateLog "status write failed: $_"
    }
    Write-UpdateLog "[$Stage step=$Step/$TotalSteps] $Message"
}

function Invoke-Step {
    param(
        [Parameter(Mandatory)][string]$Stage,
        [Parameter(Mandatory)][string]$Message,
        [Parameter(Mandatory)][int]$Step,
        [Parameter(Mandatory)][int]$TotalSteps,
        [Parameter(Mandatory)][scriptblock]$Script
    )
    Write-Status -Stage $Stage -Message $Message -Step $Step -TotalSteps $TotalSteps
    try {
        $out = & $Script 2>&1
        $rc  = $LASTEXITCODE
    } catch {
        Add-Tail $_.ToString()
        throw "$Message failed: $_"
    }
    if ($null -ne $out) { Add-Tail (($out | Out-String).Trim()) }
    if ($null -ne $rc -and $rc -ne 0) { throw "$Message exited with code $rc" }
    return $out
}

function Resolve-PythonExe {
    $py = 'C:\Windows\py.exe'
    if (Test-Path $py) { return $py }
    foreach ($c in @(
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python313\python.exe'),
        (Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'),
        'C:\Program Files\Python313\python.exe',
        'C:\Program Files\Python312\python.exe'
    )) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue |
        Where-Object { $_.Source -notmatch 'WindowsApps' } |
        Select-Object -First 1
    if ($cmd) { return $cmd.Source }
    return $null
}

# ---------- main ----------

$script:startedAt = (Get-Date).ToString('o')
$totalSteps = 6
Write-UpdateLog "==== update.ps1 starting (uvicorn pid=$UvicornPid, repo=$RepoRoot) ===="

try {
    Push-Location $RepoRoot

    # Step 1: capture pre-update state for change detection.
    Write-Status -Stage 'fetching' -Message 'Reading current revision' -Step 1 -TotalSteps $totalSteps
    $preSha = (& git rev-parse HEAD).Trim()
    Add-Tail "current HEAD: $preSha"

    # Step 2: fetch + hard-reset.
    Invoke-Step -Stage 'fetching' -Message "Fetching $Remote/$Branch" -Step 2 -TotalSteps $totalSteps -Script {
        & git fetch --quiet $Remote $Branch
    } | Out-Null

    $targetSha = (& git rev-parse "$Remote/$Branch").Trim()
    Write-Status -Stage 'fetching' -Message "Resetting to $($targetSha.Substring(0,7))" -Step 2 -TotalSteps $totalSteps -TargetSha $targetSha
    Invoke-Step -Stage 'fetching' -Message "Resetting working tree" -Step 2 -TotalSteps $totalSteps -Script {
        & git reset --hard "$Remote/$Branch"
    } | Out-Null

    $changedFiles = (& git diff --name-only "$preSha" HEAD) -split "`r?`n" | Where-Object { $_ }
    Add-Tail ("changed files: " + ($changedFiles.Count) + " files")

    # Step 3: pip deps if requirements.txt changed (or if it's the first run
    # with the file present and we can't tell yet).
    $reqFile  = Join-Path $RepoRoot 'Trade_Perf\requirements.txt'
    $reqChanged = ($changedFiles -contains 'Trade_Perf/requirements.txt')
    if ($reqChanged -and (Test-Path $reqFile)) {
        $py = Resolve-PythonExe
        if (-not $py) { throw 'Could not locate a service-usable python.exe' }
        Invoke-Step -Stage 'pip' -Message 'Installing Python dependencies' -Step 3 -TotalSteps $totalSteps -Script {
            & $py -m pip install --quiet --upgrade -r $reqFile
        } | Out-Null
    } else {
        Write-Status -Stage 'pip' -Message 'No Python dep changes; skipping pip install' -Step 3 -TotalSteps $totalSteps
    }

    # Step 4: npm install if package-lock.json changed.
    $webDir   = Join-Path $RepoRoot 'Trade_Perf\dashboard\web'
    $lockPath = 'Trade_Perf/dashboard/web/package-lock.json'
    $pkgPath  = 'Trade_Perf/dashboard/web/package.json'
    $depsChanged = ($changedFiles -contains $lockPath) -or ($changedFiles -contains $pkgPath)
    if ($depsChanged) {
        Invoke-Step -Stage 'npm' -Message 'Installing frontend dependencies' -Step 4 -TotalSteps $totalSteps -Script {
            Push-Location $webDir
            try { & npm install --no-audit --no-fund } finally { Pop-Location }
        } | Out-Null
    } else {
        Write-Status -Stage 'npm' -Message 'No frontend dep changes; skipping npm install' -Step 4 -TotalSteps $totalSteps
    }

    # Step 5: rebuild frontend (always -- TS/CSS changes don't trigger npm i
    # but still need a build).
    Invoke-Step -Stage 'build' -Message 'Building React frontend' -Step 5 -TotalSteps $totalSteps -Script {
        Push-Location $webDir
        try { & npm run build } finally { Pop-Location }
    } | Out-Null

    # Step 6: signal "done" BEFORE killing uvicorn so the next API instance
    # has the right status to return when the frontend resumes polling.
    Write-Status -Stage 'done' -Message 'Update applied; restarting service' -Step 6 -TotalSteps $totalSteps -TargetSha $targetSha

    Start-Sleep -Seconds $RestartGraceSeconds
    Write-UpdateLog "killing uvicorn pid=$UvicornPid; watchdog will respawn"
    try { Stop-Process -Id $UvicornPid -Force -ErrorAction Stop }
    catch { Write-UpdateLog "Stop-Process failed (uvicorn may already be down): $_" }

    Pop-Location
    exit 0
}
catch {
    $errMsg = $_.Exception.Message
    Add-Tail "FAILED: $errMsg"
    Write-Status -Stage 'failed' -Message 'Update failed' -Step 0 -TotalSteps $totalSteps -Error $errMsg
    Write-UpdateLog "FAILED: $errMsg"
    try { Pop-Location } catch { }
    exit 1
}
