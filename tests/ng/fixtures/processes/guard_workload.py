"""Small IPC-controlled workloads; no arbitrary timers or external resources."""
from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
from pathlib import Path


def main():
    mode, ready_path, marker_name = sys.argv[1:4]
    marker = Path(marker_name)
    child = None
    stopping = False
    fd_results = []
    if mode == "fdcheck":
        for value in sys.argv[4:6]:
            try:
                os.fstat(int(value))
                fd_results.append("open")
            except OSError:
                fd_results.append("closed")

    def stopped(signum, _frame):
        nonlocal stopping
        if stopping:
            return
        stopping = True
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        marker.write_text(str(signum))
        raise SystemExit(0)

    if mode in {"signal", "grandchild", "escaped"}:
        signal.signal(signal.SIGINT, stopped)
        signal.signal(signal.SIGTERM, stopped)
    elif mode == "ignore":
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode in {"grandchild", "escaped"}:
        child = subprocess.Popen(
            (sys.executable, __file__, "ignore", ready_path, str(marker) + ".child"),
            start_new_session=(mode == "escaped"), close_fds=True,
        )
    marker.write_text("started")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
        peer.connect(ready_path)
        peer.sendall(json.dumps({"pid": os.getpid(), "pgid": os.getpgrp(),
                                 "mode": mode, "child": child.pid if child else None}).encode() + b"\n")
        if mode == "fdcheck":
            marker.write_text(json.dumps({"fds": fd_results, "argv": sys.argv[6:],
                                          "env": os.environ.get("LITERAL_TEST"),
                                          "private_env": sorted(k for k in os.environ if k.startswith("GUARD_"))}))
            os.write(1, b"literal stdout\n")
            os.write(2, b"literal stderr\n")
            return
        # Signal handlers interrupt this wait. The peer also releases the
        # process during failed-test cleanup, so test failures cannot leak it.
        peer.recv(1)
    if child is not None:
        child.wait(timeout=45)
    marker.write_text("finished")


if __name__ == "__main__":
    main()
