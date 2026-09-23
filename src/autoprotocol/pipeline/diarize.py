"""Local community-1 CPU probe on normalized PCM WAV; no cloud inference."""

import argparse
import json
import os
import time
import wave
from pathlib import Path

REQUIRED_FILES = (
    "config.yaml",
    "segmentation/pytorch_model.bin",
    "embedding/pytorch_model.bin",
    "plda/plda.npz",
    "plda/xvec_transform.npz",
)


def diarize(audio: Path, model: Path) -> dict:
    if not audio.is_file():
        raise FileNotFoundError(f"Audio file not found: {audio}")
    for name in REQUIRED_FILES:
        if not (model / name).is_file():
            raise FileNotFoundError(f"Local model file missing: {model / name}")
    with wave.open(str(audio), "rb") as source:
        if (source.getnchannels(), source.getframerate(), source.getsampwidth()) != (1, 16000, 2):
            raise ValueError("Expected mono 16 kHz PCM16 WAV; normalize with FFmpeg first")
        duration = source.getnframes() / source.getframerate()
        if duration <= 0 or duration > 600:
            raise ValueError("This diagnostic probe supports 0 < duration <= 600 seconds")
        pcm = source.readframes(source.getnframes())

    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["PYANNOTE_METRICS_ENABLED"] = "0"
    cache = Path("data/cache/matplotlib").resolve()
    cache.mkdir(parents=True, exist_ok=True)
    os.environ.setdefault("MPLCONFIGDIR", str(cache))
    import numpy as np
    import psutil
    import torch
    from pyannote.audio import Pipeline

    torch.set_num_threads(4)
    waveform = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768)
    start = time.perf_counter()
    pipeline = Pipeline.from_pretrained(str(model.resolve()))
    pipeline.to(torch.device("cpu"))
    loaded = time.perf_counter()
    # In-memory decoding avoids dependency on system FFmpeg shared DLLs / torchcodec.
    output = pipeline({"waveform": waveform.unsqueeze(0), "sample_rate": 16000})
    finished = time.perf_counter()

    def serialize(annotation):
        return [
            {
                "start_ms": round(turn.start * 1000),
                "end_ms": round(turn.end * 1000),
                "speaker_id": speaker,
            }
            for turn, _, speaker in annotation.itertracks(yield_label=True)
        ]

    turns = serialize(output.speaker_diarization)
    memory = psutil.Process().memory_info()
    return {
        "device": "cpu",
        "cpu_threads": 4,
        "duration_seconds": duration,
        "load_seconds": round(loaded - start, 3),
        "inference_seconds": round(finished - loaded, 3),
        "total_seconds": round(finished - start, 3),
        "process_peak_wset_mib": (
            round(memory.peak_wset / 1024**2, 1) if hasattr(memory, "peak_wset") else None
        ),
        "speaker_count": len({turn["speaker_id"] for turn in turns}),
        "turns": turns,
        "exclusive_turns": serialize(output.exclusive_speaker_diarization),
        "speaker_names_confirmed": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("audio", type=Path)
    parser.add_argument("--model", type=Path, default=Path("models/pyannote-community-1"))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() == args.audio.resolve():
        parser.error("Output must not overwrite input audio")
    try:
        result = diarize(args.audio, args.model)
    except (FileNotFoundError, ImportError, ValueError, wave.Error) as error:
        parser.exit(2, f"{error}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in ("turns", "exclusive_turns")}))


if __name__ == "__main__":
    main()
