"""Run the local web server and one worker; stop both on exit."""

import argparse
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

import psutil

from autoprotocol.config import Settings

HOST = "127.0.0.1"


def stop(process):
    if process.poll() is None:
        try:
            parent = psutil.Process(process.pid)
            targets = parent.children(recursive=True) + [parent]
            for target in reversed(targets):
                try:
                    target.terminate()
                except psutil.NoSuchProcess:
                    pass
            _, alive = psutil.wait_procs(targets, timeout=3)
            for target in alive:
                try:
                    target.kill()
                except psutil.NoSuchProcess:
                    pass
        except psutil.NoSuchProcess:
            pass
        process.wait(timeout=5)


def port_is_free(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        if probe.connect_ex((HOST, port)) == 0:
            return False
    with socket.socket() as probe:
        try:
            probe.bind((HOST, port))
        except OSError:
            return False
    return True


def choose_port(preferred, attempts=20):
    for port in range(preferred, min(preferred + attempts, 65536)):
        if port_is_free(port):
            return port
    last = preferred + attempts - 1
    raise SystemExit(f"Ports {preferred}-{last} are busy; set AUTOPROTOCOL_PORT")


def server_is_live(url):
    # Bypass any system proxy: the server listens on loopback only.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"{url}/health/live", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--open-browser", action="store_true", help="open the page once the server answers"
    )
    parser.add_argument(
        "--auto-port", action="store_true", help="use the next free port if the configured is busy"
    )
    args = parser.parse_args(argv)
    children = []
    exit_code = 0
    config = Settings()
    port = choose_port(config.port) if args.auto_port else config.port
    url = f"http://{HOST}:{port}"
    if port != config.port:
        print(f"Port {config.port} is busy, using {port}", flush=True)
    browser_pending = args.open_browser
    try:
        for command in (
            ["-m", "autoprotocol.worker"],
            [
                "-m",
                "uvicorn",
                "autoprotocol.main:create_app",
                "--factory",
                "--host",
                HOST,
                "--port",
                str(port),
                "--no-access-log",
            ],
        ):
            children.append(subprocess.Popen([sys.executable, *command], start_new_session=True))
        while all(child.poll() is None for child in children):
            if browser_pending and server_is_live(url):
                browser_pending = False
                print(f"\nJazAI: {url}  (Ctrl+C - stop)\n", flush=True)
                webbrowser.open(url)
            time.sleep(0.5)
        exit_code = next((child.returncode for child in children if child.returncode), 1)
    except KeyboardInterrupt:
        pass
    finally:
        for child in children:
            stop(child)
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
