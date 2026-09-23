$ErrorActionPreference = 'Stop'
Set-Location (Split-Path $PSScriptRoot -Parent)
Add-Type -AssemblyName System.Speech
$speech = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $speech.SelectVoice('Microsoft Irina Desktop')
    New-Item -ItemType Directory -Force data/smoke | Out-Null
    $output = Join-Path (Get-Location) 'data\smoke\ru.wav'
    $speech.SetOutputToWaveFile($output)
    $speech.Speak((Get-Content tests/fixtures/ru_smoke.txt -Raw -Encoding UTF8))
} finally {
    $speech.Dispose()
}
Write-Host 'Created data/smoke/ru.wav using local Windows speech synthesis.'
