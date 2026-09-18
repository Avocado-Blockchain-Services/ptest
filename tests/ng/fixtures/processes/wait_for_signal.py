"""Fixture child which has no timer and exits only from the guard's signal."""
from __future__ import annotations

import signal
import socket
from pathlib import Path
import sys


def stopped(_signum, _frame) -> None:
    Path(sys.argv[1]).write_text("terminated")
    raise SystemExit(0)


signal.signal(signal.SIGTERM, stopped)
Path(sys.argv[1]).write_text("started")
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ready:
    ready.connect(sys.argv[2])
signal.pause()
