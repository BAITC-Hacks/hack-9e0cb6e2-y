$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) { throw 'Run scripts/setup.ps1 first.' }
# Start from installed packages without network access.
& $python -m autoprotocol.launch
exit $LASTEXITCODE
