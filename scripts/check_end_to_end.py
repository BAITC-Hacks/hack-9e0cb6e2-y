"""Fresh local audio -> real models -> saved review -> both DOCX templates.

Uses a separate database and processes. Synthetic fixtures only by default.
Does not download models, use stored model answers, or modify the normal app database.
"""

import argparse
import hashlib
import io
import ipaddress
import json
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import wave
from pathlib import Path

import psutil
from check_offline_guard import guard_environment, summarize_guard_logs
from docx import Document

from autoprotocol.analysis_review import ActionEdit
from autoprotocol.launch import stop


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, default=Path("data/smoke/ru.wav"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--offline-guard", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    folder = args.output_dir.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    audio = args.audio.resolve()
    if not audio.is_file():
        parser.error("Prepare the synthetic WAV first with scripts/synthesize_smoke.ps1")
    report = {
        "passed": False,
        "audio_sha256": hashlib.sha256(audio.read_bytes()).hexdigest(),
        "checks": {},
        "durations_seconds": {},
        "network": {
            "os_network_isolation": False,
            "python_guard_requested": args.offline_guard,
            "sampling_interval_seconds": 0.5,
            "external_connections_observed": [],
            "observed_process_names": [],
            "inspection_errors": 0,
            "note": "Socket sampling cannot prove absence of short-lived native connections.",
        },
    }
    env = {
        **os.environ,
        "AUTOPROTOCOL_DATA_DIR": str(folder / "app-data"),
        "AUTOPROTOCOL_MODELS_DIR": str(root / "models"),
        "HF_HUB_OFFLINE": "1",
        "HF_HUB_DISABLE_TELEMETRY": "1",
        "PYANNOTE_METRICS_ENABLED": "0",
        "PYTHONIOENCODING": "utf-8",
    }
    if args.offline_guard:
        env = guard_environment(folder / "network-guard", base=env)
    with socket.socket() as available:
        available.bind(("127.0.0.1", 0))
        port = available.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def http(path, payload=None, *, method=None, headers=None, expected=200, binary=False):
        headers = headers or {}
        if payload is not None and not isinstance(payload, bytes):
            payload = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(base + path, data=payload, method=method, headers=headers)
        try:
            response = opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            content = response.read()
            if response.status != expected:
                raise RuntimeError(
                    f"HTTP check failed: {method or 'GET'} {path.split('?')[0]} "
                    f"returned {response.status}, expected {expected}"
                )
            return content if binary else json.loads(content)

    def wait_for(path, states, timeout=900):
        deadline = time.monotonic() + timeout
        previous = None
        while time.monotonic() < deadline:
            current = http(path)
            state = (current.get("status"), current.get("stage"))
            if state != previous:
                print(json.dumps({"status": state[0], "stage": state[1]}), flush=True)
                previous = state
            if current["status"] in states:
                return current
            time.sleep(1)
        raise RuntimeError("End-to-end stage timed out")

    children = []
    done = threading.Event()
    process_names, external = set(), set()
    peak_rss = 0

    def monitor():
        nonlocal peak_rss
        while not done.is_set():
            seen = {}
            for child in children:
                try:
                    process = psutil.Process(child.pid)
                    for p in [process, *process.children(recursive=True)]:
                        seen[p.pid] = p
                except psutil.Error:
                    continue
            total = 0
            for p in seen.values():
                try:
                    process_names.add(p.name())
                    total += p.memory_info().rss
                    for connection in p.net_connections(kind="inet"):
                        if (
                            connection.raddr
                            and not ipaddress.ip_address(connection.raddr.ip).is_loopback
                        ):
                            external.add((p.name(), connection.raddr.ip, connection.raddr.port))
                except (psutil.Error, ValueError):
                    report["network"]["inspection_errors"] += 1
            peak_rss = max(peak_rss, total)
            done.wait(0.5)

    thread = threading.Thread(target=monitor, daemon=True)
    started = time.monotonic()
    try:
        for command in (
            [
                "-m",
                "uvicorn",
                "autoprotocol.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--no-access-log",
            ],
            ["-m", "autoprotocol.worker"],
        ):
            children.append(
                subprocess.Popen(
                    [sys.executable, *command],
                    env=env,
                    cwd=root,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
            )
        thread.start()
        for _ in range(150):
            try:
                http("/health/live")
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("Acceptance server did not start")
        query = urllib.parse.urlencode(
            {
                "title": "Синтетический сквозной прогон",
                "consent": "true",
                "timezone": "Asia/Qyzylorda",
                "occurred_on": "2026-09-23",
            }
        )
        upload_headers = {"Content-Type": "application/octet-stream"}
        http("/api/meetings?" + query, b"not a recording", expected=422, headers=upload_headers)
        report["checks"]["corrupted_media_rejected"] = True
        key = str(uuid.uuid4())
        upload_headers["Idempotency-Key"] = key
        speech_started = time.monotonic()
        meeting = http(
            "/api/meetings?" + query, audio.read_bytes(), expected=202, headers=upload_headers
        )
        repeated = http(
            "/api/meetings?" + query, audio.read_bytes(), expected=202, headers=upload_headers
        )
        assert meeting["id"] == repeated["id"]
        report["checks"]["upload_idempotent"] = True
        identifier = meeting["id"]
        report["meeting_id"] = identifier
        path = f"/api/meetings/{identifier}"
        ready = wait_for(path, {"ready", "failed"})
        if ready["status"] != "ready":
            raise RuntimeError(ready.get("error") or "Audio pipeline failed")
        report["durations_seconds"]["audio_pipeline"] = round(time.monotonic() - speech_started, 3)
        transcript = http(path + "/result")
        (folder / "transcript-original.json").write_text(
            json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        assert transcript["segments"]
        report["checks"]["fresh_transcript"] = True
        report["segments"] = len(transcript["segments"])
        original = transcript["review"]
        segment = transcript["segments"][0]
        patch = {
            "source_digest": original["source_digest"],
            "expected_revision": original["revision"],
            "segments": [
                {k: segment[k] for k in ("id", "text", "speaker_id")} | {"reviewed": True}
            ],
        }
        if segment["speaker_id"]:
            patch["participants"] = [
                {
                    "speaker_id": segment["speaker_id"],
                    "display_name": "Синтетический голос Irina",
                    "confirmed": True,
                }
            ]
        saved = http(path + "/result", patch, method="PATCH")
        assert saved["review"]["revision"] == original["revision"] + 1
        assert http(path + "/result")["segments"][0]["reviewed"]
        report["checks"]["transcript_review_persisted"] = True
        analysis_started = time.monotonic()
        http(
            path + "/analysis",
            {
                "expected_revision": saved["review"]["revision"],
                "source_digest": saved["review"]["source_digest"],
            },
            expected=202,
        )
        analysis = wait_for(path + "/analysis", {"ready", "failed"})
        if analysis["status"] != "ready":
            raise RuntimeError(analysis.get("error") or "Local extraction failed")
        report["durations_seconds"]["analysis"] = round(time.monotonic() - analysis_started, 3)
        (folder / "analysis-original.json").write_text(
            json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["analysis_metadata"] = analysis["result"]["metadata"]
        report["checks"]["real_local_analysis"] = True
        if not analysis["result"]["actions"]:
            raise RuntimeError(
                "Synthetic recording contains an action, but no action was extracted"
            )
        action = {k: analysis["result"]["actions"][0][k] for k in ActionEdit.model_fields}
        action.update(task=action["task"] + " [проверка экспорта]", reviewed=True)
        reviewed = http(
            path + f"/analysis/{analysis['id']}/review",
            {"expected_revision": analysis["result"]["review"]["revision"], "actions": [action]},
            method="PATCH",
        )
        assert reviewed["actions"][0]["task"] == action["task"]
        report["checks"]["action_edit_persisted"] = True
        payload = {
            "analysis_id": analysis["id"],
            "source_digest": saved["review"]["source_digest"],
            "transcript_revision": saved["review"]["revision"],
            "analysis_revision": reviewed["review"]["revision"],
        }
        for number in ("1", "2"):
            content = http(path + "/export.docx", {**payload, "template": number}, binary=True)
            (folder / f"protocol-{number}.docx").write_bytes(content)
            document = Document(io.BytesIO(content))
            assert any(
                action["task"] in cell.text
                for table in document.tables
                for row in table.rows
                for cell in row.cells
            )
            report["checks"][f"docx_{number}_saved_edit"] = True
        http(path + "/export.docx", {**payload, "analysis_revision": 0}, expected=409)
        report["checks"]["stale_export_rejected"] = True
        silent = io.BytesIO()
        with wave.open(silent, "wb") as stream:
            stream.setparams((1, 2, 16000, 0, "NONE", "NONE"))
            stream.writeframes(b"\0\0" * 16000 * 3)
        silence_started = time.monotonic()
        silence = http(
            "/api/meetings?" + query,
            silent.getvalue(),
            expected=202,
            headers={"Content-Type": "application/octet-stream"},
        )
        silent_result = wait_for(f"/api/meetings/{silence['id']}", {"ready", "failed"})
        assert (
            silent_result["status"] == "failed" and "Речь не распознана" in silent_result["error"]
        )
        report["durations_seconds"]["silence"] = round(time.monotonic() - silence_started, 3)
        report["checks"]["silence_clear_failure"] = True
        report["passed"] = True
    except Exception as error:
        report["error"] = str(error)
        raise
    finally:
        done.set()
        if thread.is_alive():
            thread.join(timeout=5)
        for child in reversed(children):
            stop(child)
        report["network"]["external_connections_observed"] = sorted(external)
        report["network"]["observed_process_names"] = sorted(process_names)
        if args.offline_guard:
            guard = summarize_guard_logs(folder / "network-guard")
            report["network"]["python_guard"] = guard
            report["checks"]["python_guard_logs_active"] = guard["active"]
            report["checks"]["no_external_connections_observed"] = not external
            if not guard["active"] or external:
                report["passed"] = False
                report.setdefault("error", "Guard logs inactive or external connection observed")
        report["process_tree_peak_rss_mib"] = round(peak_rss / 2**20, 1)
        report["durations_seconds"]["total"] = round(time.monotonic() - started, 3)
        (folder / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(
            json.dumps({"passed": report["passed"], "report": str(folder / "report.json")}),
            flush=True,
        )
    if not report["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
