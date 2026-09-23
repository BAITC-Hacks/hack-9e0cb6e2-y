import json
from subprocess import CompletedProcess

import pytest

from autoprotocol.media import probe


@pytest.mark.parametrize(
    ("duration", "streams"),
    [
        ("nan", [{"codec_type": "audio"}]),
        ("inf", [{"codec_type": "audio"}]),
        ("0", [{"codec_type": "audio"}]),
        ("12", []),
    ],
)
def test_probe_rejects_invalid_metadata(tmp_path, monkeypatch, duration, streams):
    metadata = json.dumps({"format": {"duration": duration}, "streams": streams}).encode()
    monkeypatch.setattr(
        "autoprotocol.media.subprocess.run",
        lambda *args, **kwargs: CompletedProcess([], 0, stdout=metadata),
    )
    with pytest.raises(ValueError, match="Не удалось прочитать"):
        probe(tmp_path / "source", "ffprobe", 600)


def test_probe_rejects_long_recording(tmp_path, monkeypatch):
    metadata = json.dumps(
        {"format": {"duration": "601"}, "streams": [{"codec_type": "audio"}]}
    ).encode()
    monkeypatch.setattr(
        "autoprotocol.media.subprocess.run",
        lambda *args, **kwargs: CompletedProcess([], 0, stdout=metadata),
    )
    with pytest.raises(ValueError, match="длиннее"):
        probe(tmp_path / "source", "ffprobe", 600)
