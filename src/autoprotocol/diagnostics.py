"""Проверка инструментов, без скачивания моделей или внешних запросов."""

import json
import platform
import shutil

from autoprotocol.config import Settings


def main() -> None:
    config = Settings()
    print(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": platform.system(),
                "device_configured": config.device,
                "ffmpeg_available": shutil.which("ffmpeg") is not None,
                "ffprobe_available": shutil.which("ffprobe") is not None,
                "models_directory_exists": config.models_dir.is_dir(),
                "pipeline_implemented": False,
                "note": "Наличие каталога не подтверждает готовность моделей",
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
