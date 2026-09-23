"""Download pinned community-1 weights using the local Hugging Face login."""

import hashlib
import json
import os
from pathlib import Path

os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"

from huggingface_hub import snapshot_download  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "pyannote/speaker-diarization-community-1"
REVISION = "3533c8cf8e369892e6b79ff1bf80f7b0286a54ee"


def main():
    destination = ROOT / "models" / "pyannote-community-1"
    snapshot_download(
        MODEL_ID,
        revision=REVISION,
        local_dir=destination,
        allow_patterns=["*.yaml", "*.bin", "*.npz", "*.md"],
    )
    files = []
    for path in sorted(destination.rglob("*")):
        if path.is_file() and ".cache" not in path.relative_to(destination).parts:
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files.append(
                {
                    "name": path.relative_to(destination).as_posix(),
                    "bytes": path.stat().st_size,
                    "sha256": digest,
                }
            )
    manifest = {"model_id": MODEL_ID, "revision": REVISION, "license": "CC-BY-4.0", "files": files}
    (ROOT / "models" / "diarization-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps({"model_id": MODEL_ID, "revision": REVISION, "files": len(files)}))


if __name__ == "__main__":
    main()
