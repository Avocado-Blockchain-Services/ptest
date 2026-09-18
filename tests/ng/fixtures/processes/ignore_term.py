"""Fixture child that proves the guard's grace-expiry kill targets its own group."""
from __future__ import annotations

import signal
import socket
from pathlib import Path
import sys


signal.signal(signal.SIGTERM, signal.SIG_IGN)
Path(sys.argv[1]).write_text("started")
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ready:
    ready.connect(sys.argv[2])
signal.pause()
