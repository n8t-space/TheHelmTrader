# install.ps1 -- The Helm one-shot installer
#
# Run from elevated PowerShell, inside a TheHelmTrader checkout:
#
#     git clone https://github.com/n8t-space/TheHelmTrader.git
#     cd TheHelmTrader
#     # Right-click PowerShell -> Run as administrator
#     .\install.ps1
#
# What it does:
#   1. Verifies (or installs via winget) Python 3.12+, Node LTS, Git, NSSM
#   2. pip-installs the dashboard's Python deps
#   3. Builds the React frontend (Vite -> dashboard/web/dist/)
#   4. Copies the NinjaScript indicators into NT8's user folder
#   5. Registers the recorder.py startup shortcut + launches it now
#   6. Registers the HelmDashboardWatchdog NSSM service + starts it
#
# Idempotent: re-runnable. Each step skips work already done.
#
# Switches:
#   -SkipPrereqs        Don't try to winget-install missing tools
#   -SkipNsIndicators   Don't touch NT8's Indicators folder
#   -SkipRecorder       Don't install the recorder startup shortcut
#   -SkipService        Don't install the NSSM service
#
# ASCII-only by design (PS 5.1 silently mishandles non-ASCII in scripts).

[CmdletBinding()]
param(
    [switch]$SkipPrereqs,
    [switch]$SkipNsIndicators,
    [switch]$SkipRecorder,
    [switch]$SkipService
)

$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot

# PS 5.1's console defaults to the OEM codepage (typically Windows-1252 / CP437)
# and mangles UTF-8 output from native commands -- vite prints checkmarks and
# box-drawing chars during the frontend build that come out as "Gamma-pound-chi"
# noise without this. PS 7+ does it by default; this fixes 5.1.
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
$OutputEncoding = [System.Text.Encoding]::UTF8

# PS 5.1 wraps every line of a native command's stderr as a NativeCommandError
# when '2>&1' is in play, which trips $ErrorActionPreference = 'Stop' even on
# successful builds (vite's "chunks > 500 kB" advisory is the classic example).
# Use this wrapper for any native exe whose stderr is noisy-but-not-fatal --
# it temporarily drops EAP, runs the command, and only throws on a real
# non-zero exit code.
function Invoke-NativeCapture {
    param(
        [Parameter(Mandatory)][scriptblock]$Block,
        [string]$ErrorMessage = 'native command failed'
    )
    $prev = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        $out = & $Block 2>&1
        $rc  = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $prev
    }
    if ($rc -ne 0 -and $null -ne $rc) {
        throw "$ErrorMessage (exit $rc)"
    }
    return $out
}

# ----- Sanity ----------------------------------------------------------------

if (-not (Test-Path "$here\TradingBot\app\src") -or -not (Test-Path "$here\Trade_Perf\dashboard\api")) {
    throw "install.ps1 must run from the root of a TheHelmTrader checkout. Got: $here"
}

$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) {
    throw "Run from elevated PowerShell (right-click PowerShell or Terminal -> Run as administrator)."
}

Write-Host "==================================================="
Write-Host " The Helm installer"
Write-Host " Checkout : $here"
Write-Host "==================================================="

# ----- 1. Prerequisites ------------------------------------------------------

function Test-Tool { param([string]$Name) [bool](Get-Command $Name -ErrorAction SilentlyContinue) }

function Show-ToolVersion { param([string]$Name, [string]$Arg = '--version')
    try {
        $v = (& $Name $Arg 2>&1 | Select-Object -First 1)
        Write-Host "   $Name : $v"
    } catch {
        Write-Host "   $Name : (version probe failed)"
    }
}

if (-not $SkipPrereqs) {
    Write-Host ""
    Write-Host "[1/6] Verifying prerequisites ..."

    $needsWinget = $false
    $tools = @(
        @{ Cmd = 'python'; Pkg = 'Python.Python.3.12' },
        @{ Cmd = 'node';   Pkg = 'OpenJS.NodeJS.LTS'  },
        @{ Cmd = 'git';    Pkg = 'Git.Git'             },
        @{ Cmd = 'nssm';   Pkg = 'NSSM.NSSM'           }
    )
    foreach ($t in $tools) {
        if (Test-Tool $t.Cmd) {
            $arg = if ($t.Cmd -eq 'nssm') { 'version' } else { '--version' }
            Show-ToolVersion $t.Cmd $arg
        } else {
            Write-Host "   $($t.Cmd) NOT FOUND -- installing $($t.Pkg) via winget ..."
            & winget install --id $t.Pkg --silent --accept-package-agreements --accept-source-agreements
            $needsWinget = $true
        }
    }

    if ($needsWinget) {
        # Refresh PATH so the newly installed tools are visible in this session.
        $env:Path = [Environment]::GetEnvironmentVariable('Path','Machine') + ';' +
                    [Environment]::GetEnvironmentVariable('Path','User')
    }
} else {
    Write-Host ""
    Write-Host "[1/6] Skipping prerequisite check (per -SkipPrereqs)."
}

# ----- 2. Python deps --------------------------------------------------------

Write-Host ""
Write-Host "[2/6] Installing Python dependencies ..."
$pipOut = Invoke-NativeCapture { python -m pip install --upgrade pip } 'pip self-upgrade failed'
$pipOut | Where-Object { $_ -match 'Successfully|already' } | ForEach-Object { Write-Host "   $_" }
$pipOut = Invoke-NativeCapture { python -m pip install fastapi 'uvicorn[standard]' pydantic requests Pillow httpx tzdata } 'pip install failed'
$pipOut | Where-Object { $_ -match 'Successfully|already|ERROR' } | ForEach-Object { Write-Host "   $_" }

# ----- 3. Build frontend -----------------------------------------------------

Write-Host ""
Write-Host "[3/6] Building React frontend ..."
Push-Location "$here\Trade_Perf\dashboard\web"
try {
    if (-not (Test-Path 'node_modules')) {
        Write-Host "   npm install ..."
        $npmOut = Invoke-NativeCapture { npm install } 'npm install failed'
        $npmOut | Select-Object -Last 6 | ForEach-Object { Write-Host "   $_" }
    } else {
        Write-Host "   node_modules present; skipping npm install"
    }
    Write-Host "   npm run build ..."
    # vite's "chunks > 500 kB" advisory goes to stderr -- Invoke-NativeCapture
    # treats it as advisory unless exit code is non-zero.
    $buildOut = Invoke-NativeCapture { npm run build } 'npm run build failed'
    $buildOut | Select-Object -Last 8 | ForEach-Object { Write-Host "   $_" }
    if (-not (Test-Path 'dist\index.html')) {
        throw "Frontend build did not produce dist\index.html."
    }
} finally {
    Pop-Location
}

# ----- 4. NinjaScript indicators --------------------------------------------

if (-not $SkipNsIndicators) {
    Write-Host ""
    Write-Host "[4/6] Copying NinjaScript indicators ..."
    $ntRoot = "$env:USERPROFILE\Documents\NinjaTrader 8"
    if (-not (Test-Path $ntRoot)) {
        Write-Warning "   NT user folder not found at $ntRoot -- skipping indicator copy."
        Write-Warning "   Install NinjaTrader 8, run it once to seed ~/Documents/NinjaTrader 8/, then re-run with -SkipPrereqs."
    } else {
        $src = "$here\TradingBot\ninjascript\_Helm Locker"
        $dst = "$ntRoot\bin\Custom\Indicators\_Helm Locker"
        New-Item -ItemType Directory -Force -Path $dst | Out-Null
        Copy-Item -Recurse -Force "$src\*" $dst
        Write-Host "   indicators copied to: $dst"
        Write-Host "   NEXT: In NinjaTrader, NinjaScript Editor (F11) -> Compile (F5)"
    }
} else {
    Write-Host ""
    Write-Host "[4/6] Skipping NinjaScript indicators (per -SkipNsIndicators)."
}

# ----- 5. Recorder startup shortcut + launch now -----------------------------

if (-not $SkipRecorder) {
    Write-Host ""
    Write-Host "[5/6] Installing recorder.py startup shortcut ..."
    & "$here\Trade_Perf\install_startup.ps1"
    # Launch it now so you don't have to log out/in.
    $startup = [Environment]::GetFolderPath('Startup')
    $shortcut = Join-Path $startup 'NT8 Trade Recorder.lnk'
    if (Test-Path $shortcut) {
        $shell = New-Object -ComObject WScript.Shell
        $lnk = $shell.CreateShortcut($shortcut)
        # Avoid duplicate launch: check if recorder is already running.
        $running = Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*recorder.py*' }
        if (-not $running) {
            Start-Process $lnk.TargetPath -ArgumentList $lnk.Arguments -WorkingDirectory $lnk.WorkingDirectory
            Write-Host "   recorder launched (will run silently in background)"
        } else {
            Write-Host "   recorder already running (PID $($running.ProcessId)) -- not re-launching"
        }
    }
} else {
    Write-Host ""
    Write-Host "[5/6] Skipping recorder (per -SkipRecorder)."
}

# ----- 6. NSSM service -------------------------------------------------------

if (-not $SkipService) {
    Write-Host ""
    Write-Host "[6/6] Installing HelmDashboardWatchdog NSSM service ..."
    Write-Host "      (will prompt for your Windows password for the service account)"
    & "$here\Trade_Perf\runtime\install_service.ps1"
} else {
    Write-Host ""
    Write-Host "[6/6] Skipping NSSM service (per -SkipService)."
}

# ----- Done ------------------------------------------------------------------

Write-Host ""
Write-Host "==================================================="
Write-Host " Install complete."
Write-Host ""
Write-Host " Dashboard once NinjaTrader is running:"
Write-Host "   http://127.0.0.1:8000/"
Write-Host ""
Write-Host " First-run config (open Settings in the dashboard):"
Write-Host "   - AI Backend : set Ollama URL + model"
Write-Host "   - Accounts   : map your NT account IDs into Live / Evals / Sim"
Write-Host ""
Write-Host " Settings file: $env:USERPROFILE\.helm\settings.json"
Write-Host "==================================================="
