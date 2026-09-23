"""Prove Python-only network blocking, without changing the PC's network settings."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

COUNTER_KEYS = {
    f"socket.{event}.{outcome}"
    for event in (
        "bind",
        "connect",
        "sendto",
        "sendmsg",
        "getaddrinfo",
        "gethostbyname",
        "gethostbyaddr",
        "getnameinfo",
    )
    for outcome in ("allowed", "blocked")
}

PROBE = r"""
import json
import socket
import subprocess
import sys
import sitecustomize

assert sitecustomize.ACTIVE
blocked = []
for label, operation in [
    ("tcp_external", lambda: socket.socket().connect(("192.0.2.1", 443))),
    ("dns_external", lambda: socket.getaddrinfo("offline-probe.invalid", 443)),
    ("udp_external", lambda: socket.socket(type=socket.SOCK_DGRAM).sendto(
        b"synthetic offline probe", ("192.0.2.1", 53))),
]:
    try:
        operation()
    except PermissionError:
        blocked.append(label)
    else:
        raise AssertionError(label + " was not blocked")
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    with socket.create_connection(listener.getsockname(), timeout=2) as client:
        with listener.accept()[0] as accepted:
            client.sendall(b"local")
            assert accepted.recv(5) == b"local"
child = subprocess.run(
    [sys.executable, "-c", "import sitecustomize; assert sitecustomize.ACTIVE"],
    check=True, capture_output=True, text=True,
)
print(json.dumps({"blocked_probes": blocked, "loopback_ok": True, "child_guard_ok": True}))
"""


def guard_environment(log_dir, base=None):
    """Environment for a Python launcher and its normally started children."""
    env = dict(os.environ if base is None else base)
    guard = Path(__file__).resolve().parent / "offline_guard"
    previous = env.get("PYTHONPATH")
    env["PYTHONPATH"] = str(guard) + (os.pathsep + previous if previous else "")
    env["AUTOPROTOCOL_OFFLINE_GUARD"] = "1"
    env["AUTOPROTOCOL_OFFLINE_GUARD_LOG_DIR"] = str(Path(log_dir).resolve())
    env["HF_HUB_OFFLINE"] = "1"
    env["HF_HUB_DISABLE_TELEMETRY"] = "1"
    env["TRANSFORMERS_OFFLINE"] = "1"
    return env


def summarize_guard_logs(log_dir):
    """Aggregate trusted-shaped counters, never arbitrary log strings or payloads.

    This confirms only the Python processes represented by these snapshots.
    Callers must use an isolated directory and separately account for their
    expected processes; native children cannot be inferred from this summary.
    """
    reasons, versions, pids, counters = set(), set(), set(), {}
    invalid_logs = 0
    try:
        directory = Path(log_dir)
        if not directory.is_dir():
            reasons.add("log_directory_missing")
            files = []
        else:
            files = list(directory.glob("python-*.json"))
            if not files:
                reasons.add("no_guard_logs")
    except OSError:
        files = []
        reasons.add("log_directory_unreadable")

    for path in files:
        try:
            if path.stat().st_size > 65536:
                raise ValueError("oversized")
            record = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(record, dict):
                raise ValueError("invalid_shape")
            version, pid = record.get("guard_version"), record.get("pid")
            if type(version) is not int or version not in {1, 2}:
                raise ValueError("unsupported_version")
            if type(pid) is not int or pid <= 0 or path.name != f"python-{pid}.json":
                raise ValueError("invalid_pid")
            if type(record.get("active")) is not bool:
                raise ValueError("invalid_active")
            counts = record.get("counters")
            if not isinstance(counts, dict) or any(
                name not in COUNTER_KEYS or type(count) is not int or count < 0
                for name, count in counts.items()
            ):
                raise ValueError("invalid_counters")
        except (OSError, UnicodeError, ValueError, RecursionError):
            invalid_logs += 1
            reasons.add("invalid_guard_log")
            continue
        if not record["active"]:
            reasons.add("inactive_guard_log")
        versions.add(version)
        pids.add(pid)
        for name, count in counts.items():
            counters[name] = counters.get(name, 0) + count

    return {
        "active": bool(pids) and not reasons,
        "guard_versions": sorted(versions),
        "pids": sorted(pids),
        "process_count": len(pids),
        "counters": dict(sorted(counters.items())),
        "invalid_log_count": invalid_logs,
        "reasons": sorted(reasons),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log-dir", type=Path, default=Path("data/offline-guard-probe"))
    args = parser.parse_args()
    result = subprocess.run(
        [sys.executable, "-c", PROBE],
        env=guard_environment(args.log_dir),
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    report = json.loads(result.stdout)
    report["scope"] = "Python socket instrumentation only; native code is not isolated"
    report["log_dir"] = str(args.log_dir.resolve())
    report["guard_summary"] = summarize_guard_logs(args.log_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not report["guard_summary"]["active"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
