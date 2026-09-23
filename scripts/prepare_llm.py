"""Online preparation of pinned public weights and a Windows CPU llama.cpp build."""

import argparse
import hashlib
import json
import shutil
import urllib.request
import zipfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MODEL_ID = "Qwen/Qwen3-4B-GGUF"
REVISION = "bc640142c66e1fdd12af0bd68f40445458f3869b"
FILENAME = "Qwen3-4B-Q4_K_M.gguf"
MODEL_SHA = "7485fe6f11af29433bc51cab58009521f205840f5b4ae3a32fa7f92e8534fdf5"
BUILD = "b11124"
ARCHIVE = f"llama-{BUILD}-bin-win-cpu-x64.zip"
ARCHIVE_SHA = "7eb4e7475f1730e0845e079e41f2e79b0c6de71d86731f755197129620a5bd28"


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def download(opener, url, path, expected=None):
    if path.is_file() and (expected is None or digest(path) == expected):
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_suffix(path.suffix + ".partial")
    offset = partial.stat().st_size if partial.exists() else 0
    headers = {"User-Agent": "JazAI-model-preparation"}
    if offset:
        headers["Range"] = f"bytes={offset}-"
    with opener.open(urllib.request.Request(url, headers=headers), timeout=120) as response:
        append = response.status == 206 and response.headers.get("Content-Range", "").startswith(
            f"bytes {offset}-"
        )
        with partial.open("ab" if append else "wb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
    if expected and digest(partial) != expected:
        raise RuntimeError(f"SHA-256 mismatch: {path.name}; remove the partial file and retry")
    partial.replace(path)
    print(f"Prepared {path.name}: {path.stat().st_size} bytes", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--direct", action="store_true", help="Download without environment proxy")
    args = parser.parse_args()
    opener = urllib.request.build_opener(
        *([urllib.request.ProxyHandler({})] if args.direct else [])
    )
    runtime = ROOT / "models" / f"llama-{BUILD}"
    archive = runtime / ARCHIVE
    download(
        opener,
        f"https://github.com/ggml-org/llama.cpp/releases/download/{BUILD}/{ARCHIVE}",
        archive,
        ARCHIVE_SHA,
    )
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            target = (runtime / info.filename).resolve()
            if not target.is_relative_to(runtime.resolve()):
                raise RuntimeError("Unsafe runtime archive path")
        bundle.extractall(runtime)
    model = ROOT / "models" / "qwen3-4b"
    for name, checksum in ((FILENAME, MODEL_SHA), ("README.md", None), ("LICENSE", None)):
        download(
            opener,
            f"https://huggingface.co/{MODEL_ID}/resolve/{REVISION}/{name}",
            model / name,
            checksum,
        )
    files = [
        {
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "bytes": path.stat().st_size,
            "sha256": digest(path),
        }
        for folder in (model, runtime)
        for path in sorted(folder.rglob("*"))
        if path.is_file()
    ]
    manifest = {
        "prepared_at": datetime.now(UTC).isoformat(),
        "model_id": MODEL_ID,
        "revision": REVISION,
        "license": "Apache-2.0",
        "runtime": "llama.cpp",
        "runtime_build": BUILD,
        "runtime_license": "MIT",
        "files": files,
    }
    (ROOT / "models" / "llm-manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print("LLM preparation complete", flush=True)


if __name__ == "__main__":
    main()
