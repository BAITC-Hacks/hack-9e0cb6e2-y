import json
import math
import subprocess
from pathlib import Path

# No playlists, image sequences or network protocols. Browser filenames are not trusted.
FORMATS = "mp3,wav,mov,matroska,webm,ogg,flac,aac"


def probe(path: Path, executable: str, duration_limit: int) -> float:
    try:
        process = subprocess.run(
            [
                executable,
                "-v",
                "error",
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                FORMATS,
                "-select_streams",
                "a",
                "-show_entries",
                "format=duration:stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            timeout=30,
            check=True,
        )
        info = json.loads(process.stdout)
        duration = float(info["format"]["duration"])
        if not info.get("streams") or not math.isfinite(duration) or duration <= 0:
            raise ValueError
    except FileNotFoundError as error:
        raise RuntimeError(
            "FFprobe не найден. Установите FFmpeg и перезапустите приложение."
        ) from error
    except (subprocess.SubprocessError, ValueError, KeyError) as error:
        raise ValueError(
            "Не удалось прочитать запись. Проверьте формат и наличие аудиодорожки."
        ) from error
    if duration > duration_limit:
        raise ValueError(f"Запись длиннее допустимых {duration_limit // 60} минут.")
    return duration
