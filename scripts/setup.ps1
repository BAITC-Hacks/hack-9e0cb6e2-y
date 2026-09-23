param([switch]$WithML)
$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw 'Install uv: python -m pip install uv==0.8.22'
}
if ($WithML) { uv sync --frozen --extra stt --extra diarization }
else { uv sync --frozen }
if ($LASTEXITCODE -ne 0) { throw 'Dependency installation failed.' }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
Write-Host 'Ready. Start: .\scripts\run.ps1'
