"""Run the local web server and one worker; stop both on exit."""

import subprocess
import sys
import time

import psutil

from autoprotocol.config import Settings


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


def main():
    children = []
    exit_code = 0
    config = Settings()
    try:
        for args in (
            ["-m", "autoprotocol.worker"],
            [
                "-m",
                "uvicorn",
                "autoprotocol.main:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(config.port),
                "--no-access-log",
            ],
        ):
            children.append(subprocess.Popen([sys.executable, *args], start_new_session=True))
        while all(child.poll() is None for child in children):
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
