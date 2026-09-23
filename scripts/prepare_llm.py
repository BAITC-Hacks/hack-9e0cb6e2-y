"""Online preparation of pinned public weights and a CPU llama.cpp build for this OS."""

import argparse
import hashlib
import json
import platform
import shutil
import sys
import tarfile
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
# Official CPU builds of the same release; SHA-256 from the GitHub release metadata.
ARCHIVES = {
    ("win32", "x64"): (
        "win-cpu-x64.zip",
        "7eb4e7475f1730e0845e079e41f2e79b0c6de71d86731f755197129620a5bd28",
    ),
    ("win32", "arm64"): (
        "win-cpu-arm64.zip",
        "120f055384dd090094699ed5f4a6636755dbfc04bb1199ea7a0d9bc8f2a5ad6f",
    ),
    ("linux", "x64"): (
        "ubuntu-x64.tar.gz",
        "104ec3bf8d2e355c0bb13bb08a8d2593f2365e42d843d09426a3fdab8c668d74",
    ),
    ("linux", "arm64"): (
        "ubuntu-arm64.tar.gz",
        "88ebc7ed72618855688296228a1af2998e8249601a04bd62bae93c654c199f28",
    ),
    ("darwin", "arm64"): (
        "macos-arm64.tar.gz",
        "fc8665397c647b04eacf62b01a262e15f7da407a796e7809e8c9d2efbdbb4124",
    ),
    ("darwin", "x64"): (
        "macos-x64.tar.gz",
        "ad9112aa0f425926943834345fde852106b6ae7befed7e6e85a5387be1dea0fd",
    ),
}


def runtime_archive():
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine)
    system = "linux" if sys.platform.startswith("linux") else sys.platform
    if (system, arch) not in ARCHIVES:
        raise SystemExit(f"No pinned llama.cpp build for {sys.platform}/{machine}")
    suffix, checksum = ARCHIVES[(system, arch)]
    return f"llama-{BUILD}-bin-{suffix}", checksum


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
    name, checksum = runtime_archive()
    archive = runtime / name
    download(
        opener,
        f"https://github.com/ggml-org/llama.cpp/releases/download/{BUILD}/{name}",
        archive,
        checksum,
    )
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                target = (runtime / info.filename).resolve()
                if not target.is_relative_to(runtime.resolve()):
                    raise RuntimeError("Unsafe runtime archive path")
            bundle.extractall(runtime)
    else:
        # Linux/macOS archives contain one llama-<build>/ folder with symlinked libraries.
        with tarfile.open(archive) as bundle:
            names = [m.name.rstrip("/") for m in bundle.getmembers()]
            if any(n != runtime.name and not n.startswith(f"{runtime.name}/") for n in names):
                raise RuntimeError("Unexpected runtime archive layout")
            bundle.extractall(runtime.parent, filter="data")
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
