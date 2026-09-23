"""Opt-in Python-only network guard for reproducible offline acceptance checks.

Put this directory first in PYTHONPATH and set AUTOPROTOCOL_OFFLINE_GUARD=1.
Normal Python children inherit it; Python -I/-E/-S and native code do not.
This is test instrumentation, not an OS network sandbox or a security boundary.
"""

import atexit
import ipaddress
import json
import os
import sys
import threading
from pathlib import Path

GUARD_VERSION = 2
ACTIVE = False


def _literal_loopback(host):
    if isinstance(host, bytes):
        try:
            host = host.decode("ascii")
        except UnicodeDecodeError:
            return False
    if not isinstance(host, str) or "%" in host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _install():
    global ACTIVE
    # Resolve and verify the sink before installing the hook. Misconfiguration
    # must terminate this test process, not silently start it without a guard.
    configured = os.environ.get("AUTOPROTOCOL_OFFLINE_GUARD_LOG_DIR")
    if not configured:
        raise RuntimeError("AUTOPROTOCOL_OFFLINE_GUARD_LOG_DIR is required")
    directory = Path(configured).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"python-{os.getpid()}.json"
    temporary = target.with_suffix(".tmp")
    counters = {}
    lock = threading.RLock()
    summary = {
        "guard_version": GUARD_VERSION,
        "pid": os.getpid(),
        "ppid": os.getppid(),
        "scope": "Python socket instrumentation only; not OS network isolation",
        "active": True,
        "counters": counters,
    }

    def snapshot():
        # Per-process atomic snapshots also survive forced worker shutdown.
        # No hostnames, paths, request payloads, tokens or transcript are logged.
        with lock:
            temporary.write_text(json.dumps(summary, sort_keys=True), encoding="utf-8")
            os.replace(temporary, target)

    def record(event, allowed):
        with lock:
            key = f"{event}.{'allowed' if allowed else 'blocked'}"
            counters[key] = counters.get(key, 0) + 1
            snapshot()

    def audit(event, args):
        if event in {"socket.bind", "socket.connect", "socket.sendto", "socket.sendmsg"}:
            address = args[1]
            allowed = isinstance(address, tuple) and bool(address) and _literal_loopback(address[0])
        elif event in {"socket.getaddrinfo", "socket.gethostbyname"}:
            allowed = _literal_loopback(args[0])
        elif event in {"socket.gethostbyaddr", "socket.getnameinfo"}:
            # Reverse lookups may issue DNS even for numeric input. These APIs
            # are unnecessary for the local HTTP pipeline and remain blocked.
            allowed = False
        else:
            return
        record(event, allowed)
        if not allowed:
            raise PermissionError("Offline test guard blocked a non-loopback network operation")

    # CPython resolves a hostname inside connect/sendto before emitting its
    # audit event. Check Python-facing methods first to prevent that DNS step;
    # the audit hook remains a backstop and records successful local calls.
    import _socket
    import socket

    original_socket = _socket.socket

    def check_address(sock, address, event):
        if not (isinstance(address, tuple) and address and _literal_loopback(address[0])):
            audit(event, (sock, address))

    def connect(sock, address):
        check_address(sock, address, "socket.connect")
        return original_socket.connect(sock, address)

    def bind(sock, address):
        check_address(sock, address, "socket.bind")
        return original_socket.bind(sock, address)

    def connect_ex(sock, address):
        check_address(sock, address, "socket.connect")
        return original_socket.connect_ex(sock, address)

    def sendto(sock, *args):
        check_address(sock, args[-1] if args else None, "socket.sendto")
        return original_socket.sendto(sock, *args)

    def sendmsg(sock, *args):
        check_address(sock, args[3] if len(args) > 3 else None, "socket.sendmsg")
        return original_socket.sendmsg(sock, *args)

    class GuardedSocket(original_socket):
        pass

    methods = {"bind": bind, "connect": connect, "connect_ex": connect_ex, "sendto": sendto}
    if hasattr(original_socket, "sendmsg"):
        methods["sendmsg"] = sendmsg
    for name, method in methods.items():
        setattr(GuardedSocket, name, method)
        setattr(socket.socket, name, method)
    _socket.socket = GuardedSocket
    socket.SocketType = GuardedSocket

    def resolution_wrapper(original, event, always_block=False):
        def resolve(*args, **kwargs):
            host = args[0] if args else kwargs.get("host")
            if always_block or not _literal_loopback(host):
                # Reverse DNS is forbidden even when the argument is numeric.
                audit(event, (host,))
            return original(*args, **kwargs)

        return resolve

    for module in (_socket, socket):
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            event = "socket.gethostbyname" if name.endswith("_ex") else f"socket.{name}"
            setattr(module, name, resolution_wrapper(getattr(module, name), event))
        for name in ("gethostbyaddr", "getnameinfo"):
            setattr(module, name, resolution_wrapper(getattr(module, name), f"socket.{name}", True))

    if sys.platform == "win32":
        # Windows asyncio calls native ConnectEx/WSASendTo instead of socket's
        # methods. Instrument its standard proactor entry points as well.
        from asyncio.windows_events import IocpProactor

        original_connect = IocpProactor.connect
        original_sendto = IocpProactor.sendto

        def proactor_connect(proactor, conn, address):
            audit("socket.connect", (conn, address))
            return original_connect(proactor, conn, address)

        def proactor_sendto(proactor, conn, buf, flags=0, addr=None):
            audit("socket.sendto", (conn, addr))
            return original_sendto(proactor, conn, buf, flags, addr)

        IocpProactor.connect = proactor_connect
        IocpProactor.sendto = proactor_sendto

    snapshot()
    sys.addaudithook(audit)
    atexit.register(snapshot)
    ACTIVE = True


if os.environ.get("AUTOPROTOCOL_OFFLINE_GUARD") == "1":
    try:
        _install()
    except Exception:
        # site.py normally catches sitecustomize exceptions and continues.
        # An explicit opt-in with a broken guard must instead fail closed.
        sys.stderr.write("Offline test guard could not initialize; refusing to start.\n")
        os._exit(78)
