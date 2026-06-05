#Requires -Version 5.1
<#
.SYNOPSIS
    Install the repo's git hooks into .git/hooks. Run once per clone.
.DESCRIPTION
    Copies scripts/pre-push to .git/hooks/pre-push so the preflight gate runs
    before every push. Idempotent.
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$src  = Join-Path $repo 'scripts/pre-push'
$dst  = Join-Path $repo '.git/hooks/pre-push'

if (-not (Test-Path (Split-Path $dst))) {
    throw "Not a git repo (no .git/hooks). Run from inside the clone."
}
Copy-Item -Path $src -Destination $dst -Force
Write-Host "Installed pre-push hook -> $dst" -ForegroundColor Green
