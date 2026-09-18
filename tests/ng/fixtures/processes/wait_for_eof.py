"""Fixture child that exits only after its owned readiness peer closes."""
from __future__ import annotations

import socket
from pathlib import Path
import sys


Path(sys.argv[1]).write_text("started")
with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as ready:
    ready.connect(sys.argv[2])
    ready.recv(1)
Path(sys.argv[1]).write_text("finished")
