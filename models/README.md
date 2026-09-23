# Локальные веса

Скачанные веса размещаются здесь и игнорируются Git.
Порядок подготовки, лицензии и ограничения: [docs/MODELS.md](../docs/MODELS.md).
В репозитории веса и runtime не публикуются; проверка существования папки не проверяет inference.

`scripts/prepare_llm.py` готовит Qwen3-4B Q4_K_M и Windows CPU runtime llama.cpp
b11124 в подкаталогах `qwen3-4b/` и `llama-b11124/`. Во время анализа сеть для
скачивания моделей не используется. Точные SHA-256 — в локальном `llm-manifest.json`
и опубликованном `docs/llm-model-manifest.json`.
