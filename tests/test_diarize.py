import wave

import pytest

from autoprotocol.pipeline.diarize import REQUIRED_FILES, diarize


def test_diarization_missing_weights_fails_without_loading_libraries(tmp_path):
    audio = tmp_path / "input.wav"
    audio.touch()
    with pytest.raises(FileNotFoundError, match="Local model file missing"):
        diarize(audio, tmp_path / "missing-model")


def test_diarization_rejects_wrong_sample_rate_before_loading_models(tmp_path):
    model = tmp_path / "model"
    for name in REQUIRED_FILES:
        path = model / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    audio = tmp_path / "input.wav"
    with wave.open(str(audio), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(b"\x00\x00" * 8000)
    with pytest.raises(ValueError, match="Expected mono 16 kHz"):
        diarize(audio, model)
