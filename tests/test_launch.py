import socket
import subprocess
import sys

from autoprotocol.launch import choose_port, port_is_free, stop


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


def test_choose_port_skips_a_busy_port():
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        taken = busy.getsockname()[1]
        assert not port_is_free(taken)
        assert choose_port(taken) != taken
