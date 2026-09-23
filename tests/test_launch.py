import subprocess
import sys

from autoprotocol.launch import stop


def test_stop_terminates_owned_child_process():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        stop(process)
        assert process.poll() is not None
        stop(process)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
