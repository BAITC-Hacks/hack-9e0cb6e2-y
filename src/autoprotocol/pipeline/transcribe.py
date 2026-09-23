"""Standalone local STT probe; not a complete meeting pipeline."""

import argparse
import json
import os
import time
from pathlib import Path


def transcribe(audio: Path, model: Path, language: str | None = None) -> dict:
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    for name in ("model.bin", "config.json", "tokenizer.json"):
        if not (model / name).is_file():
            raise FileNotFoundError(f"Local model file missing: {model / name}")
    # Set before importing any model libraries; no model ids or remote endpoints.
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    import psutil
    from faster_whisper import WhisperModel

    start = time.perf_counter()
    engine = WhisperModel(
        str(model.resolve()),
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        local_files_only=True,
    )
    loaded = time.perf_counter()
    segments, info = engine.transcribe(
        str(audio.resolve()),
        language=language,
        beam_size=5,
        vad_filter=True,
        word_timestamps=True,
    )
    result = []
    for index, segment in enumerate(segments):
        result.append(
            {
                "id": f"seg_{index + 1:04d}",
                "start_ms": round(segment.start * 1000),
                "end_ms": round(segment.end * 1000),
                "speaker_id": None,
                "text": segment.text.strip(),
                "words": [
                    {
                        "start_ms": round(w.start * 1000),
                        "end_ms": round(w.end * 1000),
                        "text": w.word,
                    }
                    for w in segment.words or []
                ],
            }
        )
    finished = time.perf_counter()
    memory = psutil.Process().memory_info()
    return {
        "device": "cpu",
        "compute_type": "int8",
        "cpu_threads": 4,
        "detected_language": info.language,
        "duration_seconds": info.duration,
        "load_seconds": round(loaded - start, 3),
        "inference_seconds": round(finished - loaded, 3),
        "total_seconds": round(finished - start, 3),
        "process_rss_mib_at_end": round(memory.rss / 1024**2, 1),
        "process_peak_wset_mib": (
            round(memory.peak_wset / 1024**2, 1) if hasattr(memory, "peak_wset") else None
        ),
        "diarization_performed": False,
        "segments": result,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", type=Path, default=Path("models/faster-whisper-small"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--language", choices=["ru", "kk"])
    args = parser.parse_args()
    if args.output.resolve() == args.audio.resolve():
        parser.error("Output must not overwrite input audio")
    try:
        result = transcribe(args.audio, args.model, args.language)
    except (FileNotFoundError, ImportError) as error:
        parser.exit(2, f"{error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # Transcript stays in the local output artifact, not the technical log.
    print(json.dumps({k: v for k, v in result.items() if k != "segments"}))


if __name__ == "__main__":
    main()
