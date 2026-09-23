from pathlib import Path

import pytest

from autoprotocol.pipeline.transcribe import transcribe


def test_missing_model_fails_before_import_or_download(tmp_path):
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"test")
    with pytest.raises(FileNotFoundError, match="Local model file missing"):
        transcribe(audio, tmp_path / "missing-model")


def test_missing_audio_fails_before_import_or_download(tmp_path):
    with pytest.raises(FileNotFoundError, match="Audio file not found"):
        transcribe(tmp_path / "missing.wav", Path("models/faster-whisper-small"))
