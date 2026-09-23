#!/usr/bin/env bash
# JazAI: установка при первом запуске и старт приложения на macOS и Linux.
# Запускается из start-macos.command или start-linux.sh. Повторный запуск
# пропускает уже выполненные шаги. Совместим с bash 3.2 из macOS.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
PY="$ROOT/.venv/bin/python"
HF="$ROOT/.venv/bin/hf"
MODELS="$ROOT/models"
OS="$(uname -s)"
ARCH="$(uname -m)"

step() { printf '\n\033[36m[%s/6] %s\033[0m\n' "$1" "$2"; }
fail() { printf '\n\033[31mОШИБКА: %s\033[0m\n' "$1"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
files_exist() {
    for f in "$@"; do [ -e "$MODELS/$f" ] || return 1; done
}
open_url() {
    if [ "$OS" = Darwin ]; then open "$1" >/dev/null 2>&1 || true
    elif have xdg-open; then xdg-open "$1" >/dev/null 2>&1 || true
    fi
}

printf '\033[32mJazAI — локальное автопротоколирование совещаний\033[0m\n'
echo 'Первый запуск скачивает программы и модели (около 10 ГБ) и может занять 15–40 минут.'
echo 'Следующие запуски сразу открывают приложение.'

case "$OS/$ARCH" in
    Darwin/arm64)
        major="$(sw_vers -productVersion | cut -d. -f1)"
        [ "$major" -ge 14 ] || fail 'Нужна macOS 14 Sonoma или новее.' ;;
    Darwin/*)
        fail 'Нужен Mac на Apple Silicon (M1 и новее): PyTorch больше не выпускается для Intel Mac.' ;;
    Linux/x86_64 | Linux/aarch64) ;;
    *)
        fail "Система $OS/$ARCH не поддерживается. Нужны Windows x64, macOS Apple Silicon или Linux x86_64/ARM64." ;;
esac

step 1 'Установщик пакетов uv'
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
if ! have uv; then
    echo 'Устанавливаю uv 0.8.22 с astral.sh...'
    if have curl; then curl -LsSf https://astral.sh/uv/0.8.22/install.sh | sh
    elif have wget; then wget -qO- https://astral.sh/uv/0.8.22/install.sh | sh
    else fail 'Нужна программа curl или wget. Установите её и запустите скрипт снова.'
    fi
    have uv || fail 'uv не установился. Откройте новый терминал и запустите скрипт снова.'
fi
echo 'uv готов.'

step 2 'FFmpeg для чтения аудио и видео'
if ! { have ffmpeg && have ffprobe; }; then
    if [ "$OS" = Darwin ]; then
        for brew in /opt/homebrew/bin/brew /usr/local/bin/brew; do
            if ! have brew && [ -x "$brew" ]; then eval "$("$brew" shellenv)"; fi
        done
        have brew || fail 'Нужен Homebrew: установите его командой с https://brew.sh и запустите файл снова.'
        brew install ffmpeg
    else
        echo 'Устанавливаю FFmpeg. Система может спросить пароль администратора (sudo).'
        if have apt-get; then sudo apt-get update && sudo apt-get install -y ffmpeg
        elif have dnf; then sudo dnf install -y ffmpeg-free || sudo dnf install -y ffmpeg
        elif have pacman; then sudo pacman -S --needed --noconfirm ffmpeg
        elif have zypper; then sudo zypper install -y ffmpeg
        else fail 'Установите пакет ffmpeg пакетным менеджером системы и запустите скрипт снова.'
        fi
    fi
    { have ffmpeg && have ffprobe; } || fail 'FFmpeg не найден после установки.'
fi
echo 'FFmpeg готов.'

step 3 'Python 3.11 и библиотеки (uv скачает Python сам, если его нет)'
uv sync --frozen --extra stt --extra diarization ||
    fail 'Не удалось установить библиотеки. Проверьте интернет и запустите скрипт снова.'
[ -f .env ] || cp .env.example .env
# Скачивание моделей требует сети; сама обработка записей потом идёт офлайн.
export HF_HUB_OFFLINE=0

step 4 'Модель распознавания речи Whisper (~500 МБ)'
if ! files_exist faster-whisper-small/model.bin faster-whisper-small/tokenizer.json; then
    "$PY" scripts/prepare_stt.py ||
        fail 'Не удалось скачать Whisper. Проверьте интернет и запустите скрипт снова.'
fi
echo 'Whisper готов.'

step 5 'Модель разделения говорящих pyannote (нужен бесплатный аккаунт Hugging Face)'
if ! files_exist pyannote-community-1/config.yaml \
    pyannote-community-1/segmentation/pytorch_model.bin \
    pyannote-community-1/embedding/pytorch_model.bin \
    pyannote-community-1/plda/plda.npz \
    pyannote-community-1/plda/xvec_transform.npz; then
    url='https://huggingface.co/pyannote/speaker-diarization-community-1'
    printf '\n\033[33mАвторы модели выдают её только после принятия условий на сайте:\033[0m\n'
    echo "  1. Откройте $url (сейчас откроется само)."
    echo '     Войдите или зарегистрируйтесь на Hugging Face.'
    echo '  2. Заполните короткую форму и примите условия.'
    echo '  3. Дождитесь надписи «You have been granted access to this model».'
    open_url "$url"
    read -r -p 'Когда доступ открыт, нажмите Enter ' _
    if ! "$PY" -c 'import sys; from huggingface_hub import get_token; sys.exit(0 if get_token() else 1)'; then
        printf '\033[33mТеперь войдите в тот же аккаунт Hugging Face:\033[0m\n'
        "$HF" auth login || fail 'Вход в Hugging Face не выполнен. Запустите скрипт снова.'
    fi
    "$PY" scripts/prepare_diarization.py ||
        fail 'Не удалось скачать pyannote. Проверьте, что условия приняты в том же аккаунте, в который выполнен вход. Сменить аккаунт: .venv/bin/hf auth login --force'
fi
echo 'pyannote готов.'

step 6 'Модель поручений и саммари Qwen3-4B (~2,5 ГБ) и llama.cpp'
if ! files_exist qwen3-4b/Qwen3-4B-Q4_K_M.gguf llama-b11124/llama-server; then
    if ! "$PY" scripts/prepare_llm.py; then
        echo 'Повторяю скачивание без системного прокси...'
        "$PY" scripts/prepare_llm.py --direct ||
            fail 'Не удалось скачать Qwen3/llama.cpp. Запустите скрипт снова — загрузка продолжится.'
    fi
fi
echo 'Qwen3 готов.'

export HF_HUB_OFFLINE=1
printf '\n\033[32mВсё готово. Запускаю JazAI — браузер откроется сам.\033[0m\n'
echo 'Не закрывайте это окно, пока пользуетесь приложением. Остановка — Ctrl+C.'
exec "$PY" -m autoprotocol.launch --open-browser --auto-port
