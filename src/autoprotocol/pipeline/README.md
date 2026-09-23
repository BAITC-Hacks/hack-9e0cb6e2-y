# Граница будущего конвейера

Здесь появятся отдельные адаптеры `audio`, `transcription`, `diarization`,
`extraction`, `validation`, `export` и оркестратор. Сейчас адаптеры не реализованы.

HTTP API сохраняет файл и задачу; отдельный worker обрабатывает очередь SQLite.
Нельзя выполнять длительный ML inference в FastAPI BackgroundTasks.
Один worker последовательно загружает/освобождает модели, ограничивая RAM.
Контракты и обработка сбоев описаны в `docs/ARCHITECTURE.md`.
