# Install the Helm dashboard watchdog as a Windows Service via NSSM.
#
# Why NSSM (rather than native sc.exe service hosting):
#   PowerShell scripts can't be a Windows Service entry point on their own.
#   NSSM is a thin, robust SCM-compatible wrapper that's the industry-standard
#   way to host non-service binaries (CLIs, scripts) as Windows Services.
#
# This supersedes the older Task Scheduler approach in install_watchdog.ps1.
# Run from an ELEVATED PowerShell:
#
#     pwsh -NoProfile -ExecutionPolicy Bypass -File runtime\install_service.ps1
#
# By default the service runs as the current user (so it can see NinjaTrader.exe
# in your user session and write to ~/Documents/...). The script prompts for
# your password once; NSSM stores it encrypted in the service registry.
# Override with -RunAsLocalSystem if you want the service to run under SYSTEM
# instead -- note that LocalSystem cannot see user-session GUI processes
# without a CIM/WMI query path (current watchdog uses Get-Process).

[CmdletBinding()]
param(
    [string]$ServiceName  = 'HelmDashboardWatchdog',
    [string]$DisplayName  = 'Helm Dashboard Watchdog',
    [string]$Description  = 'Auto-starts the Helm dashboard (uvicorn :8000) when NinjaTrader is running. Lodestone & Purser.',
    [switch]$RunAsLocalSystem
)

$ErrorActionPreference = 'Stop'

# Elevation check
$current = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object Security.Principal.WindowsPrincipal($current)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Error "This script must run as Administrator. Re-run from an elevated shell."
}

$projectRoot   = (Resolve-Path "$PSScriptRoot\..").Path
$watchdog      = Join-Path $projectRoot 'runtime\watchdog.ps1'
$serviceLogDir = Join-Path $projectRoot 'data'
$null = New-Item -ItemType Directory -Force -Path $serviceLogDir | Out-Null

# ---- Step 1: Ensure NSSM is available ----

$nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
if (-not $nssm) {
    Write-Host "NSSM not found on PATH. Installing via winget..." -ForegroundColor Yellow
    winget install --id NSSM.NSSM --accept-package-agreements --accept-source-agreements --silent --disable-interactivity
    $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' + [Environment]::GetEnvironmentVariable('Path','User')
    $nssm = (Get-Command nssm -ErrorAction SilentlyContinue).Source
    if (-not $nssm) {
        Write-Error "winget install of NSSM completed but nssm is still not on PATH. Open a new shell and re-run, or install NSSM manually from https://nssm.cc/"
    }
}
Write-Host "Using NSSM at: $nssm" -ForegroundColor Cyan

# ---- Step 2: If service already exists, stop + remove it ----

$existing = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($existing) {
    Write-Host "Existing service found; stopping + removing for a clean install." -ForegroundColor Yellow
    if ($existing.Status -ne 'Stopped') {
        & $nssm stop $ServiceName | Out-Null
        Start-Sleep -Seconds 2
    }
    & $nssm remove $ServiceName confirm | Out-Null
    Start-Sleep -Seconds 1
}

# ---- Step 3: Install the service ----

# Run pwsh (PowerShell 7) if available, otherwise fall back to Windows PowerShell.
$pwshPath = (Get-Command pwsh -ErrorAction SilentlyContinue).Source
if (-not $pwshPath) {
    $pwshPath = (Get-Command powershell -ErrorAction SilentlyContinue).Source
}
if (-not $pwshPath) { Write-Error "Neither pwsh nor powershell found on PATH." }

Write-Host "Installing service '$ServiceName' wrapping $watchdog ..." -ForegroundColor Cyan
& $nssm install $ServiceName $pwshPath '-NoProfile' '-ExecutionPolicy' 'Bypass' '-File' "`"$watchdog`""
& $nssm set     $ServiceName DisplayName  $DisplayName
& $nssm set     $ServiceName Description  $Description
& $nssm set     $ServiceName Start        SERVICE_AUTO_START
& $nssm set     $ServiceName AppDirectory $projectRoot

# stdout / stderr -> log files (rotation: NSSM does not rotate; we accept a
# growing tail since this is informational. Truncate manually if it ever bites).
& $nssm set $ServiceName AppStdout (Join-Path $serviceLogDir 'service.out.log')
& $nssm set $ServiceName AppStderr (Join-Path $serviceLogDir 'service.err.log')

# Restart on failure: 5s delay, infinite retries, throttle 60s.
& $nssm set $ServiceName AppExit Default Restart
& $nssm set $ServiceName AppRestartDelay 5000
& $nssm set $ServiceName AppThrottle     60000

# ---- Step 4: User context ----

if ($RunAsLocalSystem) {
    Write-Host "Service will run as LocalSystem (note: cannot see user-session NT process via Get-Process)." -ForegroundColor Yellow
    & $nssm set $ServiceName ObjectName 'LocalSystem'
} else {
    $userName = "$env:USERDOMAIN\$env:USERNAME"
    Write-Host "Service will run as $userName. Enter your Windows password (NSSM stores it encrypted)." -ForegroundColor Cyan
    $cred = Get-Credential -UserName $userName -Message "Helm Dashboard Watchdog service account"
    if (-not $cred) { Write-Error 'No credentials provided; aborting.' }
    $plain = $cred.GetNetworkCredential().Password
    & $nssm set $ServiceName ObjectName $userName $plain
}

# ---- Step 5: Start it ----

Write-Host "Starting service..." -ForegroundColor Cyan
& $nssm start $ServiceName | Out-Null
Start-Sleep -Seconds 2

$svc = Get-Service -Name $ServiceName
Write-Host ""
Write-Host ("Service status : {0}" -f $svc.Status)        -ForegroundColor Green
Write-Host ("Service start  : {0}" -f $svc.StartType)     -ForegroundColor Green
Write-Host ("Logs           : $serviceLogDir\service.{out,err}.log + watchdog.log")
Write-Host ""
Write-Host "Inspect with: Get-Service $ServiceName ; Get-Content $serviceLogDir\watchdog.log -Tail 20"
