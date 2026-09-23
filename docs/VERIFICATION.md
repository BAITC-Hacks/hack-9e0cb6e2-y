# Проверка инфраструктурного каркаса — 23.09.2026

Окружение: Windows 10 Pro, Python 3.11.9, uv 0.8.22.

| Команда/проверка | Результат |
|---|---|
| `uv sync --frozen` и `scripts/setup.ps1` | Зависимости установлены, повторный запуск успешен |
| `ruff check src tests` | Пройдено |
| `ruff format --check src tests` | 7 файлов соответствуют форматированию |
| `python -m pytest -q` | 2 passed |
| `scripts/run.ps1` | Uvicorn запущен на 127.0.0.1:8000 |
| HTTP GET `/` и `/health/live` | 200 на реально запущенном сервере |
| `/health/ready` через TestClient | Ожидаемый 503, конвейер не реализован |
| `python -m autoprotocol.diagnostics` | CPU настроен, FFmpeg/ffprobe отсутствуют, pipeline_implemented=false |
| `git diff --check` | Ошибок пробелов нет |
| `git check-ignore` для .env, SQLite и venv | Все исключены из git |

pytest выдаёт одно предупреждение Starlette об устаревании httpx как транспорта
TestClient. Тесты проходят; при обновлении тестового стека перейти на рекомендованный
транспорт после проверки совместимости.

PowerShell-скрипты используют ASCII в служебных сообщениях: это устраняет проблему
чтения UTF-8 без BOM в Windows PowerShell 5.1. Продукт и документация — на русском.

CI для Windows/Linux добавлен, но результат удалённого запуска не проверялся.
Реальные STT, диаризация, LLM, экспорт, качество RU/KK/mixed и полный offline-проход
не запускались: это последующие фазы, а не возможности текущего каркаса.
