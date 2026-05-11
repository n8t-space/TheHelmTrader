# Create a Startup-folder shortcut that launches recorder.py in watch mode
# under pythonw.exe (no console window) at every login.
#
# Usage:
#   pwsh -ExecutionPolicy Bypass -File install_startup.ps1            # install
#   pwsh -ExecutionPolicy Bypass -File install_startup.ps1 -Uninstall # remove

[CmdletBinding()]
param(
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'

$projectDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$recorder   = Join-Path $projectDir 'recorder.py'
$startup    = [Environment]::GetFolderPath('Startup')
$shortcut   = Join-Path $startup 'NT8 Trade Recorder.lnk'

if ($Uninstall) {
    if (Test-Path $shortcut) {
        Remove-Item $shortcut -Force
        Write-Host "Removed: $shortcut"
    } else {
        Write-Host "No shortcut at $shortcut (already absent)"
    }
    return
}

if (-not (Test-Path $recorder)) {
    throw "recorder.py not found at $recorder"
}

# Find pythonw.exe (silent Python). Falls back to python.exe if not present.
$pythonwCmd = Get-Command pythonw.exe -ErrorAction SilentlyContinue
if ($pythonwCmd) {
    $pythonw = $pythonwCmd.Source
} else {
    $pythonw = (Get-Command python.exe -ErrorAction Stop).Source
    Write-Warning "pythonw.exe not found; using python.exe (a console window will appear at login)"
}

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($shortcut)
$lnk.TargetPath       = $pythonw
$lnk.Arguments        = "`"$recorder`" --watch --no-stdout"
$lnk.WorkingDirectory = $projectDir
$lnk.WindowStyle      = 7   # minimized (only matters if python.exe was used)
$lnk.Description      = 'NT8 trade recorder - polls NT8 SQLite db for fills'
$lnk.Save()

Write-Host "Installed: $shortcut"
Write-Host "  target:  $pythonw"
Write-Host "  args:    `"$recorder`" --watch --no-stdout"
Write-Host "  workdir: $projectDir"
Write-Host ""
Write-Host "It will start automatically at next login. To start it now without rebooting:"
Write-Host "  Start-Process '$pythonw' -ArgumentList '`"$recorder`"','--watch','--no-stdout' -WorkingDirectory '$projectDir'"
