import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

GUARD = Path(__file__).resolve().parents[1] / "scripts" / "offline_guard"
_SPEC = importlib.util.spec_from_file_location(
    "check_offline_guard", GUARD.parent / "check_offline_guard.py"
)
_HELPER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_HELPER)
summarize_guard_logs = _HELPER.summarize_guard_logs


def run_guard(code, log_dir, *, enabled=True, log_configured=True):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(GUARD)
    env["AUTOPROTOCOL_OFFLINE_GUARD"] = "1" if enabled else "0"
    env.pop("AUTOPROTOCOL_OFFLINE_GUARD_LOG_DIR", None)
    if log_configured:
        env["AUTOPROTOCOL_OFFLINE_GUARD_LOG_DIR"] = str(log_dir)
    return subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=15
    )


@pytest.mark.parametrize(
    "operation,event",
    [
        ('socket.socket().connect(("192.0.2.1", 443))', "socket.connect"),
        ('socket.socket().connect_ex(("192.0.2.1", 443))', "socket.connect"),
        ('socket.socket().connect(("private-text.invalid", 443))', "socket.connect"),
        ('socket.socket().connect_ex(("private-text.invalid", 443))', "socket.connect"),
        ('socket.socket().bind(("private-text.invalid", 0))', "socket.bind"),
        ('socket.getaddrinfo("private-text.invalid", 443)', "socket.getaddrinfo"),
        ('socket.getaddrinfo("localhost", 443)', "socket.getaddrinfo"),
        ('socket.gethostbyname("private-text.invalid")', "socket.gethostbyname"),
        ('socket.gethostbyname_ex("private-text.invalid")', "socket.gethostbyname"),
        ('socket.gethostbyaddr("127.0.0.1")', "socket.gethostbyaddr"),
        ('socket.getnameinfo(("127.0.0.1", 80), 0)', "socket.getnameinfo"),
        (
            'socket.socket(type=socket.SOCK_DGRAM).sendto(b"private-text", ("192.0.2.1", 53))',
            "socket.sendto",
        ),
        (
            'socket.socket(type=socket.SOCK_DGRAM).sendto(b"x", ("private-text.invalid", 53))',
            "socket.sendto",
        ),
    ],
)
def test_external_network_or_dns_is_blocked_before_contact(tmp_path, operation, event):
    result = run_guard(
        "import socket\ntry:\n    "
        + operation
        + "\nexcept PermissionError:\n    pass\nelse:\n    raise AssertionError('not blocked')",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    logs = list(tmp_path.glob("python-*.json"))
    assert len(logs) == 1
    raw = logs[0].read_text(encoding="utf-8")
    assert "private-text" not in raw
    assert json.loads(raw)["counters"] == {f"{event}.blocked": 1}


def test_loopback_http_style_connection_is_allowed(tmp_path):
    result = run_guard(
        """
import socket
import sitecustomize
assert sitecustomize.ACTIVE
with socket.socket() as listener:
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    with socket.create_connection(listener.getsockname(), timeout=2) as client:
        with listener.accept()[0] as accepted:
            client.sendall(b"private transcript text")
            assert accepted.recv(100) == b"private transcript text"
assert socket.getaddrinfo("::1", 80)
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    raw = next(tmp_path.glob("python-*.json")).read_text(encoding="utf-8")
    assert "private transcript text" not in raw
    counters = json.loads(raw)["counters"]
    assert counters["socket.connect.allowed"] == 1
    assert not any(name.endswith(".blocked") for name in counters)


def test_normal_python_child_inherits_guard(tmp_path):
    result = run_guard(
        """
import subprocess
import sys
import sitecustomize
assert sitecustomize.ACTIVE
code = "import socket; socket.getaddrinfo('child.invalid', 80)"
child = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
assert child.returncode != 0
assert "PermissionError" in child.stderr
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    logs = [json.loads(path.read_text()) for path in tmp_path.glob("python-*.json")]
    assert len(logs) == 2
    assert any(log["counters"].get("socket.getaddrinfo.blocked") == 1 for log in logs)
    # The Windows venv redirector may be an intermediate parent process.
    assert len({log["pid"] for log in logs}) == 2


def test_asyncio_external_tcp_and_udp_are_blocked(tmp_path):
    result = run_guard(
        """
import asyncio
import socket

async def main():
    loop = asyncio.get_running_loop()
    with socket.socket() as client:
        client.setblocking(False)
        try:
            await loop.sock_connect(client, ("192.0.2.1", 443))
        except PermissionError:
            pass
        else:
            raise AssertionError("async TCP was not blocked")
    with socket.socket(type=socket.SOCK_DGRAM) as client:
        client.setblocking(False)
        try:
            await loop.sock_sendto(client, b"synthetic", ("192.0.2.1", 53))
        except PermissionError:
            pass
        else:
            raise AssertionError("async UDP was not blocked")

asyncio.run(main())
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    counters = json.loads(next(tmp_path.glob("python-*.json")).read_text())["counters"]
    assert counters["socket.connect.blocked"] == 1
    assert counters["socket.sendto.blocked"] == 1


def test_inactive_guard_has_no_side_effects(tmp_path):
    result = run_guard(
        "import sitecustomize; assert not sitecustomize.ACTIVE", tmp_path, enabled=False
    )
    assert result.returncode == 0, result.stderr
    assert not list(tmp_path.iterdir())


def test_enabled_guard_without_log_sink_refuses_to_start(tmp_path):
    result = run_guard("raise AssertionError('must never run')", tmp_path, log_configured=False)
    assert result.returncode == 78
    assert "refusing to start" in result.stderr
    assert "must never run" not in result.stderr


def test_non_literal_and_non_loopback_destinations_are_rejected(tmp_path):
    result = run_guard(
        """
import sitecustomize
for host in ["127.1", "localhost", "::ffff:127.0.0.1", "::1%1", "0.0.0.0", "8.8.8.8", None]:
    assert not sitecustomize._literal_loopback(host)
for host in ["127.0.0.1", "127.0.0.2", "::1", b"127.0.0.1"]:
    assert sitecustomize._literal_loopback(host)
""",
        tmp_path,
    )
    assert result.returncode == 0, result.stderr


def write_log(directory, file_pid, **overrides):
    record = {"pid": file_pid, "guard_version": 2, "active": True, "counters": {}}
    record.update(overrides)
    path = directory / f"python-{file_pid}.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    return path


def test_guard_summary_aggregates_versions_and_safe_counts(tmp_path):
    write_log(tmp_path, 101, counters={"socket.connect.allowed": 3}, transcript="do not reveal")
    write_log(
        tmp_path,
        102,
        guard_version=1,
        counters={"socket.connect.allowed": 4, "socket.getaddrinfo.blocked": 1},
    )
    report = summarize_guard_logs(tmp_path)
    assert report == {
        "active": True,
        "guard_versions": [1, 2],
        "pids": [101, 102],
        "process_count": 2,
        "counters": {"socket.connect.allowed": 7, "socket.getaddrinfo.blocked": 1},
        "invalid_log_count": 0,
        "reasons": [],
    }
    assert "do not reveal" not in json.dumps(report)


@pytest.mark.parametrize("exists", [True, False])
def test_guard_summary_missing_logs_never_means_active(tmp_path, exists):
    report = summarize_guard_logs(tmp_path if exists else tmp_path / "missing")
    assert report["active"] is False
    assert report["process_count"] == 0
    assert report["reasons"] == ["no_guard_logs" if exists else "log_directory_missing"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"counters": {"private transcript text": 1}},
        {"counters": {"socket.connect.allowed": "private transcript text"}},
        {"counters": {"socket.connect.allowed": -1}},
        {"counters": {"socket.connect.allowed": True}},
        {"counters": []},
        {"guard_version": "private transcript text"},
        {"guard_version": 99},
        {"guard_version": True},
        {"pid": 999},
        {"active": "true"},
    ],
)
def test_invalid_guard_log_fails_summary_without_echoing_content(tmp_path, overrides):
    write_log(tmp_path, 101, **overrides)
    write_log(tmp_path, 102)
    report = summarize_guard_logs(tmp_path)
    assert report["active"] is False
    assert report["invalid_log_count"] == 1
    assert report["pids"] == [102]
    assert report["reasons"] == ["invalid_guard_log"]
    assert "private transcript text" not in json.dumps(report)


def test_inactive_guard_snapshot_fails_summary(tmp_path):
    write_log(tmp_path, 101, active=False)
    write_log(tmp_path, 102)
    report = summarize_guard_logs(tmp_path)
    assert report["active"] is False
    assert report["reasons"] == ["inactive_guard_log"]


@pytest.mark.parametrize("contents", ["private transcript text", "[]", "{}", "null"])
def test_unparseable_guard_snapshot_fails_summary(tmp_path, contents):
    (tmp_path / "python-101.json").write_text(contents, encoding="utf-8")
    report = summarize_guard_logs(tmp_path)
    assert report["active"] is False
    assert report["invalid_log_count"] == 1
    assert report["reasons"] == ["invalid_guard_log"]
    assert "private transcript text" not in json.dumps(report)
