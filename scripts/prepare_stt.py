"""Online preparation only. Downloads public weights, never meeting data."""

import hashlib
import json
import os
from pathlib import Path

os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from huggingface_hub import snapshot_download  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "Systran/faster-whisper-small"
REVISION = "536b0662742c02347bc0e980a01041f333bce120"


def main():
    destination = ROOT / "models" / "faster-whisper-small"
    manifest_path = ROOT / "models" / "stt-manifest.json"
    revision = REVISION
    snapshot_download(
        MODEL_ID,
        revision=revision,
        local_dir=destination,
        allow_patterns=["*.json", "model.bin", "vocabulary.*", "README.md"],
    )
    files = []
    for path in sorted(destination.iterdir()):
        if path.is_file():
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append({"name": path.name, "bytes": path.stat().st_size, "sha256": digest})
    manifest = {"model_id": MODEL_ID, "revision": revision, "files": files}
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(json.dumps({"model_id": MODEL_ID, "revision": revision, "files": len(files)}))


if __name__ == "__main__":
    main()
