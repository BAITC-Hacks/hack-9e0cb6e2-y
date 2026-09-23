"""Separate worker; each ML stage runs in a fresh process to release model memory."""

import argparse
import json
import subprocess
import sys
import time

from autoprotocol import jobs
from autoprotocol.config import Settings
from autoprotocol.launch import stop
from autoprotocol.media import FORMATS
from autoprotocol.storage import initialize


def run_stage(command, config, job):
    started = time.monotonic()
    next_heartbeat = started
    # Model output and subprocess exceptions never enter ordinary transcript logs.
    process = subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True
    )
    try:
        while process.poll() is None:
            now = time.monotonic()
            if now - started > config.stage_timeout_seconds:
                raise RuntimeError("Время выполнения этапа превышено.")
            if now >= next_heartbeat:
                if not jobs.heartbeat(config.database_path, job):
                    raise RuntimeError("Задание передано другому обработчику.")
                next_heartbeat = now + 10
            time.sleep(0.5)
        if process.returncode:
            raise RuntimeError("Ошибка локального обработчика. Проверьте модели и зависимости.")
    finally:
        if process.poll() is None:
            stop(process)


def process_job(config, job):
    stage = "preparing"
    try:
        if config.device != "cpu":
            raise RuntimeError("Web-конвейер пока поддерживает только CPU.")
        models = config.models_dir.resolve()
        required = [
            models / "faster-whisper-small" / "model.bin",
            models / "pyannote-community-1" / "config.yaml",
        ]
        if not all(path.is_file() for path in required):
            raise RuntimeError(
                "Локальные модели не найдены. Выполните подготовку STT и диаризации."
            )
        work = config.data_dir.resolve() / "meetings" / job["id"] / job["owner"]
        work.mkdir(parents=True, exist_ok=True)
        audio, stt, diar, result = [
            work / name for name in ("audio.wav", "stt.json", "diar.json", "result.json")
        ]
        commands = [
            (
                "normalizing",
                [
                    config.ffmpeg,
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-format_whitelist",
                    FORMATS,
                    "-i",
                    job["source_path"],
                    "-map",
                    "0:a:0",
                    "-vn",
                    "-ac",
                    "1",
                    "-ar",
                    "16000",
                    "-c:a",
                    "pcm_s16le",
                    "-y",
                    str(audio),
                ],
            ),
            (
                "transcribing",
                [
                    sys.executable,
                    "-m",
                    "autoprotocol.pipeline.transcribe",
                    str(audio),
                    "--model",
                    str(models / "faster-whisper-small"),
                    "--output",
                    str(stt),
                ],
            ),
            (
                "diarizing",
                [
                    sys.executable,
                    "-m",
                    "autoprotocol.pipeline.diarize",
                    str(audio),
                    "--model",
                    str(models / "pyannote-community-1"),
                    "--output",
                    str(diar),
                ],
            ),
            (
                "aligning",
                [
                    sys.executable,
                    "-m",
                    "autoprotocol.pipeline.align",
                    str(stt),
                    str(diar),
                    "--output",
                    str(result),
                ],
            ),
        ]
        for stage, command in commands:
            if not jobs.update(config.database_path, job, stage):
                return
            run_stage(command, config, job)
        parsed = json.loads(result.read_text(encoding="utf-8"))
        if "segments" not in parsed:
            raise RuntimeError("Не получен транскрипт.")
        parsed["meeting_id"] = job["id"]
        parsed["audio_sha256"] = job["audio_sha256"]
        result.write_text(json.dumps(parsed, ensure_ascii=False, indent=2), encoding="utf-8")
        jobs.update(
            config.database_path,
            job,
            "transcript_ready",
            status="ready",
            result=str(result),
            audio=str(audio),
        )
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, RuntimeError) else "Ошибка обработки записи."
        jobs.update(config.database_path, job, stage, status="failed", error=message)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    config = Settings()
    initialize(config.database_path)
    while True:
        job = jobs.claim(config.database_path)
        if job:
            print(f"Processing {job['id']}", flush=True)
            process_job(config, job)
        if args.once:
            return
        if not job:
            time.sleep(2)


if __name__ == "__main__":
    main()
