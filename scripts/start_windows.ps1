# JazAI: установка при первом запуске и старт приложения на Windows.
# Запускается из start-windows.bat. Повторный запуск пропускает уже выполненные шаги.
$ErrorActionPreference = 'Stop'
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Python = Join-Path $Root '.venv\Scripts\python.exe'
$Hf = Join-Path $Root '.venv\Scripts\hf.exe'
$Models = Join-Path $Root 'models'

function Step($Number, $Text) {
    Write-Host ''
    Write-Host "[$Number/6] $Text" -ForegroundColor Cyan
}

function Fail($Text) {
    Write-Host ''
    Write-Host "ОШИБКА: $Text" -ForegroundColor Red
    exit 1
}

function Refresh-Path {
    $env:Path = @(
        "$env:USERPROFILE\.local\bin",
        [Environment]::GetEnvironmentVariable('Path', 'Machine'),
        [Environment]::GetEnvironmentVariable('Path', 'User')
    ) -join ';'
}

function Test-Command($Name) {
    [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

function Find-FFmpeg {
    if ((Test-Command ffmpeg) -and (Test-Command ffprobe)) { return $true }
    # winget дописывает PATH только для новых окон; ищем установленную копию сами.
    $found = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages\Gyan.FFmpeg*\*\bin\ffprobe.exe" `
        -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($found) {
        $env:Path = "$($found.DirectoryName);$env:Path"
        return $true
    }
    return $false
}

function Test-Files($Paths) {
    foreach ($path in $Paths) {
        if (-not (Test-Path (Join-Path $Models $path))) { return $false }
    }
    return $true
}

Write-Host 'JazAI — локальное автопротоколирование совещаний' -ForegroundColor Green
Write-Host 'Первый запуск скачивает программы и модели (около 10 ГБ) и может занять 15–40 минут.'
Write-Host 'Следующие запуски сразу открывают приложение.'

Step 1 'Установщик пакетов uv'
Refresh-Path
if (-not (Test-Command uv)) {
    Write-Host 'Устанавливаю uv 0.8.22 с astral.sh...'
    Invoke-RestMethod https://astral.sh/uv/0.8.22/install.ps1 | Invoke-Expression
    Refresh-Path
    if (-not (Test-Command uv)) { Fail 'uv не установился. Закройте окно и запустите start-windows.bat ещё раз.' }
}
Write-Host 'uv готов.'

Step 2 'FFmpeg для чтения аудио и видео'
if (-not (Find-FFmpeg)) {
    if (-not (Test-Command winget)) {
        Fail ('Не найден winget. Установите «Установщик приложений» (App Installer) из Microsoft Store ' +
            'или FFmpeg вручную с https://ffmpeg.org/download.html, затем запустите файл снова.')
    }
    Write-Host 'Устанавливаю FFmpeg через winget...'
    winget install --id Gyan.FFmpeg --exact --source winget --accept-package-agreements --accept-source-agreements
    Refresh-Path
    if (-not (Find-FFmpeg)) { Fail 'FFmpeg не найден после установки. Закройте окно и запустите файл снова.' }
}
Write-Host 'FFmpeg готов.'

Step 3 'Python 3.11 и библиотеки (uv скачает Python сам, если его нет)'
uv sync --frozen --extra stt --extra diarization
if ($LASTEXITCODE -ne 0) { Fail 'Не удалось установить библиотеки. Проверьте интернет и запустите файл снова.' }
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
# Скачивание моделей требует сети; сама обработка записей потом идёт офлайн.
$env:HF_HUB_OFFLINE = '0'

Step 4 'Модель распознавания речи Whisper (~500 МБ)'
if (-not (Test-Files @('faster-whisper-small\model.bin', 'faster-whisper-small\tokenizer.json'))) {
    & $Python scripts/prepare_stt.py
    if ($LASTEXITCODE -ne 0) { Fail 'Не удалось скачать Whisper. Проверьте интернет и запустите файл снова.' }
}
Write-Host 'Whisper готов.'

Step 5 'Модель разделения говорящих pyannote (нужен бесплатный аккаунт Hugging Face)'
$diarization = @(
    'pyannote-community-1\config.yaml',
    'pyannote-community-1\segmentation\pytorch_model.bin',
    'pyannote-community-1\embedding\pytorch_model.bin',
    'pyannote-community-1\plda\plda.npz',
    'pyannote-community-1\plda\xvec_transform.npz'
)
if (-not (Test-Files $diarization)) {
    Write-Host ''
    Write-Host 'Авторы модели выдают её только после принятия условий на сайте:' -ForegroundColor Yellow
    Write-Host '  1. Сейчас откроется страница модели. Войдите или зарегистрируйтесь на Hugging Face.'
    Write-Host '  2. Заполните короткую форму и примите условия.'
    Write-Host '  3. Дождитесь надписи «You have been granted access to this model».'
    Start-Process 'https://huggingface.co/pyannote/speaker-diarization-community-1'
    Read-Host 'Когда доступ открыт, нажмите Enter'
    & $Python -c 'import sys; from huggingface_hub import get_token; sys.exit(0 if get_token() else 1)'
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Теперь войдите в тот же аккаунт Hugging Face (откроется вход через браузер):' -ForegroundColor Yellow
        & $Hf auth login
        if ($LASTEXITCODE -ne 0) { Fail 'Вход в Hugging Face не выполнен. Запустите файл снова.' }
    }
    & $Python scripts/prepare_diarization.py
    if ($LASTEXITCODE -ne 0) {
        Fail ('Не удалось скачать pyannote. Проверьте, что условия приняты в том же аккаунте, ' +
            'в который выполнен вход. Сменить аккаунт: .venv\Scripts\hf.exe auth login --force')
    }
}
Write-Host 'pyannote готов.'

Step 6 'Модель поручений и саммари Qwen3-4B (~2,5 ГБ) и llama.cpp'
if (-not (Test-Files @('qwen3-4b\Qwen3-4B-Q4_K_M.gguf', 'llama-b11124\llama-server.exe'))) {
    & $Python scripts/prepare_llm.py
    if ($LASTEXITCODE -ne 0) {
        Write-Host 'Повторяю скачивание без системного прокси...'
        & $Python scripts/prepare_llm.py --direct
        if ($LASTEXITCODE -ne 0) { Fail 'Не удалось скачать Qwen3/llama.cpp. Запустите файл снова — загрузка продолжится.' }
    }
}
Write-Host 'Qwen3 готов.'

$env:HF_HUB_OFFLINE = '1'
Write-Host ''
Write-Host 'Всё готово. Запускаю JazAI — браузер откроется сам.' -ForegroundColor Green
Write-Host 'Не закрывайте это окно, пока пользуетесь приложением. Остановка — Ctrl+C.'
& $Python -m autoprotocol.launch --open-browser --auto-port
exit $LASTEXITCODE
