"""Real scheduler guard with test-only boundary barriers/failure injection."""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import time
from pathlib import Path
from types import SimpleNamespace

from ptest import guard


def main() -> int:
    control_fd = int(os.environ.pop("GUARD_CONTROL_FD"))
    manifest_fd = int(os.environ.pop("GUARD_MANIFEST_FD"))
    stage = os.environ.pop("GUARD_BARRIER_STAGE", "")
    barrier_path = os.environ.pop("GUARD_BARRIER_PATH", "")
    failure = os.environ.pop("GUARD_FAILURE", "")
    stats_path = os.environ.pop("GUARD_STATS", "")
    advance = float(os.environ.pop("GUARD_ADVANCE_AFTER_FACTS", "0"))
    decision_timeout = os.environ.pop("GUARD_DECISION_TIMEOUT", "")
    scan_limit_after_phase = os.environ.pop(
        "GUARD_SCAN_LIMIT_AFTER_PHASE", "")
    if decision_timeout:
        guard.DEFAULT_ATTEMPT_DECISION_TIMEOUT_S = float(decision_timeout)
    clock_offset = [0.0]
    if advance:
        guard.time = SimpleNamespace(monotonic=lambda: time.monotonic() + clock_offset[0])
    reads = {"eof_reads": 0}
    original_read = os.read

    def read(fd, size):
        result = original_read(fd, size)
        if fd == control_fd and not result:
            reads["eof_reads"] += 1
        return result

    if stats_path:
        os.read = read

    def barrier(name):
        if stage == name:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as peer:
                peer.settimeout(45)
                peer.connect(barrier_path)
                peer.sendall(name.encode())
                if peer.recv(1) != b"g":
                    raise RuntimeError("fixture barrier controller disappeared")

    original_manifest = guard._read_manifest

    def manifest(*args, **kwargs):
        barrier("manifest")
        return original_manifest(*args, **kwargs)

    guard._read_manifest = manifest
    original_register = guard.scheduler.register_guard

    def register(*args):
        barrier("register")
        if failure == "register":
            raise sqlite3.OperationalError("injected registration failure")
        result = original_register(*args)
        barrier("registered")
        return result

    guard.scheduler.register_guard = register
    original_draining = guard.scheduler.mark_draining

    def draining(*args):
        barrier("drain")
        if failure == "drain":
            raise sqlite3.OperationalError("injected draining failure")
        result = original_draining(*args)
        barrier("drained")
        return result

    guard.scheduler.mark_draining = draining
    original_emit = guard._emit

    def emit(*args, **kwargs):
        result = original_emit(*args, **kwargs)
        if args[2] == "phase":
            if scan_limit_after_phase:
                guard._MAX_GROUP_SCAN = int(scan_limit_after_phase)
            barrier("fork")
        if args[2] == "runner-facts":
            clock_offset[0] += advance
        return result

    guard._emit = emit
    try:
        return guard.run_guard(control_fd, manifest_fd)
    finally:
        if stats_path:
            Path(stats_path).write_text(json.dumps(reads))


if __name__ == "__main__":
    raise SystemExit(main())
